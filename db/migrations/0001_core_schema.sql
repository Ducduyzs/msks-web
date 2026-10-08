-- MSKS — schema lõi (ARCHITECTURE.md v0.2, mục 4.3, 5.2, 5.4, 6.5, 8).
-- PostgreSQL là nguồn trạng thái chính. Ghi dữ liệu chỉ qua backend (service role / kết nối trực tiếp);
-- người dùng đã đăng nhập chỉ có quyền ĐỌC dữ liệu thuộc workspace của mình (RLS ở 0002).
--
-- Quy ước:
--   * Mọi bảng con mang workspace_id; khóa ngoại liên bảng là khóa ghép (x_id, workspace_id)
--     để một dòng không thể tham chiếu dòng của workspace khác (mục 5.2).
--   * char_*/quote_* là khoảng nửa mở [start, end) theo Unicode code point của canonical text (mục 5.4).
--   * Trạng thái dùng text + CHECK thay vì enum để migration sau dễ mở rộng.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- workspace

create table public.workspace (
  id            uuid primary key default gen_random_uuid(),
  owner_id      uuid not null references auth.users (id) on delete cascade,
  name          text not null check (char_length(btrim(name)) between 1 and 120),
  settings_json jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now()
);
create index workspace_owner_idx on public.workspace (owner_id, created_at desc);

-- -------------------------------------------------------------------- nguồn

create table public.source (
  id                  uuid primary key default gen_random_uuid(),
  workspace_id        uuid not null references public.workspace (id) on delete cascade,
  alias               text not null check (alias ~ '^S[1-9][0-9]*$'),
  kind                text not null check (kind in ('pdf', 'docx', 'html', 'markdown', 'txt', 'doi')),
  source_type         text not null default 'user_note'
                        check (source_type in ('peer_reviewed', 'preprint', 'web', 'user_note')),
  title               text not null,
  authors_json        jsonb not null default '[]'::jsonb,
  published_at        date,
  url                 text,
  doi                 text,
  origin_fingerprint  text,
  -- Nhóm nguồn gốc: phát hiện phụ thuộc, KHÔNG chứng minh độc lập (mục 6.2).
  origin_group_id     text not null,
  independence_status text not null default 'unknown' check (independence_status in ('unknown', 'reviewed')),
  grouping_version    integer not null default 1 check (grouping_version > 0),
  grouping_reason     text,
  -- Vòng đời của revision hiện tại (mục 6.5) + tombstone.
  status              text not null default 'uploaded'
                        check (status in ('uploaded', 'parsing', 'chunking', 'embedding', 'publishing',
                                          'ready', 'failed', 'cancelled', 'deleting')),
  progress            real not null default 0 check (progress between 0 and 1),
  error_json          jsonb,
  deleted_at          timestamptz,
  created_at          timestamptz not null default now(),
  unique (workspace_id, alias),
  unique (id, workspace_id),
  -- Tombstone: trạng thái 'deleting' luôn có thời điểm xóa.
  check (status <> 'deleting' or deleted_at is not null)
);
create index source_workspace_idx on public.source (workspace_id, created_at);
create index source_group_idx on public.source (workspace_id, origin_group_id);
create unique index source_doi_idx on public.source (workspace_id, lower(doi)) where doi is not null and deleted_at is null;

-- Quan hệ giữa hai nhóm nguồn, theo phạm vi claim, có người đánh giá (mục 6.2).
create table public.source_relation (
  id               uuid primary key default gen_random_uuid(),
  workspace_id     uuid not null references public.workspace (id) on delete cascade,
  group_a          text not null,
  group_b          text not null,
  relation         text not null check (relation in ('independent', 'dependent', 'duplicate')),
  claim_scope      text,
  rationale        text not null,
  reviewer_id      uuid references auth.users (id) on delete set null,
  grouping_version integer not null check (grouping_version > 0),
  created_at       timestamptz not null default now(),
  check (group_a < group_b)
);
create index source_relation_idx on public.source_relation (workspace_id, group_a, group_b, grouping_version);

-- ------------------------------------------------------- revision bất biến

create table public.document_revision (
  id                    uuid primary key default gen_random_uuid(),
  workspace_id          uuid not null,
  source_id             uuid not null,
  revision_no           integer not null check (revision_no > 0),
  mime                  text not null,
  object_key            text not null,
  sha256                text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  byte_size             bigint not null check (byte_size >= 0),
  fetched_at            timestamptz not null default now(),
  license_metadata_json jsonb not null default '{}'::jsonb,
  unique (source_id, revision_no),
  unique (id, workspace_id),
  foreign key (source_id, workspace_id) references public.source (id, workspace_id) on delete cascade
);

create table public.parse_revision (
  id                   uuid primary key default gen_random_uuid(),
  workspace_id         uuid not null,
  document_revision_id uuid not null,
  parser               text not null,
  parser_version       text not null,
  normalizer_version   text not null,
  chunker_version      text not null,
  config_hash          text not null,
  canonical_text_key   text,
  text_sha256          text,
  source_map_key       text,
  page_count           integer check (page_count >= 0),   -- null: định dạng không phân trang
  leaf_count           integer not null default 0 check (leaf_count >= 0),
  status               text not null default 'pending'
                         check (status in ('pending', 'running', 'succeeded', 'failed', 'superseded')),
  created_at           timestamptz not null default now(),
  unique (id, workspace_id),
  foreign key (document_revision_id, workspace_id)
    references public.document_revision (id, workspace_id) on delete cascade
);

create table public.index_generation (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null,
  parse_revision_id  uuid not null,
  embedding_revision text not null,
  config_hash        text not null,
  expected_points    integer not null check (expected_points >= 0),
  state              text not null default 'building'
                       check (state in ('building', 'verifying', 'active', 'retired', 'failed', 'orphaned')),
  created_at         timestamptz not null default now(),
  published_at       timestamptz,
  unique (id, workspace_id),
  foreign key (parse_revision_id, workspace_id)
    references public.parse_revision (id, workspace_id) on delete cascade,
  check ((state = 'active') <= (published_at is not null))
);
-- Mỗi parse revision có tối đa một generation đang active; query chỉ đọc generation đã publish (mục 6.5).
create unique index index_generation_active_idx on public.index_generation (parse_revision_id) where state = 'active';

-- -------------------------------------------------------------- cây node

create table public.node (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null,
  parse_revision_id  uuid not null,
  legacy_node_id     text not null,            -- SHA-1 rút gọn của edahr.hierarchy.stable_id (parity)
  level              text not null check (level in ('document', 'section', 'parent', 'child')),
  section_id         uuid,
  parent_id          uuid,                     -- cha chính để điều hướng; membership đầy đủ ở node_edge
  position           integer not null check (position >= 0),
  text               text not null,
  text_sha256        text not null,
  token_count        integer not null check (token_count >= 0),
  page_start         integer check (page_start > 0),
  page_end           integer check (page_end > 0),
  char_start         integer not null check (char_start >= 0),
  char_end           integer not null,
  paragraph_ids_json jsonb not null default '[]'::jsonb,
  bbox_json          jsonb,
  unique (parse_revision_id, legacy_node_id),
  unique (id, workspace_id),
  check (char_end >= char_start),
  check (page_start is null or page_end is null or page_end >= page_start),
  foreign key (parse_revision_id, workspace_id)
    references public.parse_revision (id, workspace_id) on delete cascade,
  foreign key (section_id, workspace_id) references public.node (id, workspace_id),
  foreign key (parent_id, workspace_id) references public.node (id, workspace_id)
);
create index node_revision_level_idx on public.node (parse_revision_id, level, position);

create table public.node_edge (
  workspace_id   uuid not null,
  parent_node_id uuid not null,
  child_node_id  uuid not null,
  ordinal        integer not null check (ordinal >= 0),
  primary key (parent_node_id, child_node_id),
  foreign key (parent_node_id, workspace_id) references public.node (id, workspace_id) on delete cascade,
  foreign key (child_node_id, workspace_id) references public.node (id, workspace_id) on delete cascade
);
create index node_edge_child_idx on public.node_edge (child_node_id);

-- --------------------------------------------------------------------- run

create table public.run (
  id                     uuid primary key default gen_random_uuid(),
  workspace_id           uuid not null references public.workspace (id) on delete cascade,
  parent_run_id          uuid,
  rerun_of               uuid,
  created_by             uuid references auth.users (id) on delete set null,
  mode                   text not null check (mode in ('qa', 'synthesis')),
  profile                text not null default 'product' check (profile in ('product', 'v11_parity')),
  query                  text not null check (char_length(query) between 3 and 4000),
  query_type             text check (query_type in ('factoid', 'explanatory', 'comparative_multi_hop', 'global_synthesis')),
  config_hash            text not null,
  code_hash              text not null,
  model_ids_json         jsonb not null default '{}'::jsonb,
  -- Chốt document/parse revision, index generation và grouping version lúc bắt đầu (mục 5.4).
  scope_snapshot_json    jsonb not null default '{}'::jsonb,
  grouping_snapshot_json jsonb not null default '{}'::jsonb,
  budget_json            jsonb not null,
  outline_json           jsonb,
  status                 text not null default 'queued'
                           check (status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')),
  answer_status          text check (answer_status in ('answered', 'insufficient_evidence',
                                                       'model_output_invalid', 'verification_unavailable')),
  cancel_requested       boolean not null default false,
  metrics_json           jsonb,
  error_code             text,
  error_json             jsonb,
  created_at             timestamptz not null default now(),
  started_at             timestamptz,
  finished_at            timestamptz,
  unique (id, workspace_id),
  foreign key (parent_run_id, workspace_id) references public.run (id, workspace_id) on delete cascade,
  foreign key (rerun_of, workspace_id) references public.run (id, workspace_id) on delete set null (rerun_of),
  check ((finished_at is not null) = (status in ('succeeded', 'partial', 'failed', 'cancelled')))
);
create index run_workspace_idx on public.run (workspace_id, created_at desc, id desc);
create index run_parent_idx on public.run (parent_run_id) where parent_run_id is not null;

create table public.context_block (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null,
  run_id             uuid not null,
  context_id         text not null check (context_id ~ '^C[1-9][0-9]*$'),
  node_id            uuid not null,
  rank               integer not null check (rank > 0),
  utility            double precision not null,
  visible_text       text not null,
  visible_sha256     text not null,
  visible_spans_json jsonb not null default '[]'::jsonb,   -- ánh xạ từng đoạn hiển thị về node gốc
  token_count        integer not null check (token_count >= 0),
  truncated          boolean not null default false,
  -- raw_ce_score, sbert_similarity, rrf_score, packing_utility (mục 7.2)
  trace_json         jsonb not null default '{}'::jsonb,
  unique (run_id, context_id),
  unique (id, workspace_id),
  foreign key (run_id, workspace_id) references public.run (id, workspace_id) on delete cascade,
  -- Run cũ giữ tham chiếu node; GC nguồn đã xóa phải che nội dung chứ không xóa node đang được trích.
  foreign key (node_id, workspace_id) references public.node (id, workspace_id) on delete restrict
);

create table public.claim (
  id                   uuid primary key default gen_random_uuid(),
  workspace_id         uuid not null,
  run_id               uuid not null,
  ordinal              integer not null check (ordinal >= 0),
  section_key          text,
  text                 text not null,
  generator_confidence real check (generator_confidence between 0 and 1),
  status               text not null check (status in ('pending', 'supported', 'corroborated', 'contested', 'rejected')),
  cross_check_status   text not null default 'not_run' check (cross_check_status in ('not_run', 'completed', 'incomplete')),
  verification_method  text not null,
  support_score        real,
  contradiction_score  real,
  rejected_reason      text,
  created_at           timestamptz not null default now(),
  unique (run_id, ordinal),
  unique (id, workspace_id),
  check ((status = 'rejected') = (rejected_reason is not null)),
  foreign key (run_id, workspace_id) references public.run (id, workspace_id) on delete cascade
);

create table public.claim_evidence (
  id                uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null,
  claim_id          uuid not null,
  node_id           uuid not null,
  context_block_id  uuid,
  role              text not null check (role in ('primary', 'corroborating', 'contradicting')),
  evidence_snapshot text not null,
  evidence_sha256   text not null,
  quote_start       integer not null check (quote_start >= 0),
  quote_end         integer not null,
  support           real not null,
  contradiction     real not null,
  verifier_revision text not null,
  position          text not null default 'exact' check (position in ('exact', 'approximate', 'unmapped')),
  check (quote_end >= quote_start),
  -- Citation chính bắt buộc gắn với khối context mà claim đã thấy (mục 5.2).
  check (role <> 'primary' or context_block_id is not null),
  foreign key (claim_id, workspace_id) references public.claim (id, workspace_id) on delete cascade,
  foreign key (node_id, workspace_id) references public.node (id, workspace_id) on delete restrict,
  foreign key (context_block_id, workspace_id) references public.context_block (id, workspace_id) on delete cascade
);
create index claim_evidence_claim_idx on public.claim_evidence (claim_id);
create index claim_evidence_node_idx on public.claim_evidence (node_id);

-- Mục báo cáo tổng hợp, render bằng template từ claim đã kiểm chứng (mục 7.3).
create table public.report_section (
  workspace_id    uuid not null,
  run_id          uuid not null,
  key             text not null,
  ordinal         integer not null check (ordinal >= 0),
  title           text not null,
  status          text not null check (status in ('complete', 'insufficient_evidence', 'failed')),
  paragraphs_json jsonb not null default '[]'::jsonb,   -- [{template_text, claim_ids}]
  primary key (run_id, key),
  unique (run_id, ordinal),
  foreign key (run_id, workspace_id) references public.run (id, workspace_id) on delete cascade
);

-- ------------------------------------------------------- job, outbox, event

create table public.job (
  id              uuid primary key default gen_random_uuid(),
  workspace_id    uuid not null references public.workspace (id) on delete cascade,
  run_id          uuid,
  target_type     text not null check (target_type in ('source', 'document_revision', 'parse_revision',
                                                       'index_generation', 'run')),
  target_id       uuid not null,
  kind            text not null check (kind in ('ingest', 'reindex', 'delete', 'run', 'gc')),
  idempotency_key text,
  request_hash    text,
  state           text not null default 'queued' check (state in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
  stage           text,
  -- attempt_version tăng mỗi lần nhận lease; worker cũ ghi với version cũ sẽ bị từ chối (mục 4.3).
  attempt_version integer not null default 0 check (attempt_version >= 0),
  retry_count     integer not null default 0 check (retry_count >= 0),
  max_attempts    integer not null default 3 check (max_attempts > 0),
  lease_until     timestamptz,
  heartbeat_at    timestamptz,
  error_code      text,
  error_json      jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (workspace_id, kind, idempotency_key),
  unique (id, workspace_id),
  foreign key (run_id, workspace_id) references public.run (id, workspace_id) on delete cascade
);
create index job_pending_idx on public.job (state, lease_until) where state in ('queued', 'running');
create index job_target_idx on public.job (target_type, target_id);

create table public.outbox_event (
  id           bigint generated always as identity primary key,
  workspace_id uuid not null references public.workspace (id) on delete cascade,
  aggregate_id uuid not null,
  event_type   text not null,
  payload_json jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  delivered_at timestamptz
);
create index outbox_undelivered_idx on public.outbox_event (id) where delivered_at is null;

-- Event đã persist để SSE phát lại theo Last-Event-ID (mục 4.3).
create table public.run_event (
  workspace_id uuid not null,
  run_id       uuid not null,
  seq          bigint not null check (seq > 0),
  event_type   text not null check (event_type in ('stage', 'claim_verified', 'section', 'done', 'error')),
  payload_json jsonb not null,
  created_at   timestamptz not null default now(),
  primary key (run_id, seq),
  foreign key (run_id, workspace_id) references public.run (id, workspace_id) on delete cascade
);

create table public.job_event (
  workspace_id uuid not null,
  job_id       uuid not null,
  seq          bigint not null check (seq > 0),
  event_type   text not null,
  payload_json jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now(),
  primary key (job_id, seq),
  foreign key (job_id, workspace_id) references public.job (id, workspace_id) on delete cascade
);

-- ---------------------------------------------------------------- helpers

create function public.touch_updated_at() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

create trigger job_touch_updated_at before update on public.job
  for each row execute function public.touch_updated_at();

-- Thêm event với seq liên tục: khóa dòng run để hai writer không cấp trùng seq.
create function public.append_run_event(p_run_id uuid, p_event_type text, p_payload jsonb)
returns bigint
language plpgsql set search_path = '' as $$
declare
  v_workspace uuid;
  v_seq bigint;
begin
  select workspace_id into v_workspace from public.run where id = p_run_id for update;
  if v_workspace is null then
    raise exception 'run % không tồn tại', p_run_id using errcode = 'no_data_found';
  end if;
  select coalesce(max(seq), 0) + 1 into v_seq from public.run_event where run_id = p_run_id;
  insert into public.run_event (workspace_id, run_id, seq, event_type, payload_json)
  values (v_workspace, p_run_id, v_seq, p_event_type, p_payload);
  return v_seq;
end;
$$;

-- Cấp alias S<n> kế tiếp trong workspace (khóa workspace để tránh cấp trùng).
create function public.next_source_alias(p_workspace_id uuid) returns text
language plpgsql set search_path = '' as $$
declare
  v_next integer;
begin
  perform 1 from public.workspace where id = p_workspace_id for update;
  select coalesce(max(substring(alias from 2)::integer), 0) + 1 into v_next
  from public.source where workspace_id = p_workspace_id;
  return 'S' || v_next;
end;
$$;
