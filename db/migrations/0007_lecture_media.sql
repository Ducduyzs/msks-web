-- MSKS — bài giảng video và profile model (LECTURE_ARCHITECTURE.md mục 4, 6, 7, 8).
--
-- Bổ sung connector media, các revision trích xuất (transcript, vùng chữ trên frame), alignment và
-- source map canonical span → segment/region/asset/thời gian. Nội dung media chỉ đọc qua FastAPI
-- (kiểm tra quyền + tombstone); người dùng không có SELECT trực tiếp trên bảng nội dung media.

-- ------------------------------------------------------------------ source

alter table public.source drop constraint source_kind_check;
alter table public.source add constraint source_kind_check
  check (kind in ('pdf', 'docx', 'html', 'markdown', 'txt', 'doi', 'video', 'youtube', 'drive'));

-- READY_LIMITED: thiếu nhánh tùy chọn (không có hình, hết budget frame...). REVIEW_REQUIRED: bản chép
-- quá kém để tự động dùng, cần người dùng sửa (mục 8).
alter table public.source drop constraint source_status_check;
alter table public.source add constraint source_status_check
  check (status in ('uploaded', 'parsing', 'chunking', 'embedding', 'publishing', 'ready', 'ready_limited',
                    'review_required', 'failed', 'cancelled', 'deleting', 'deleted'));

alter table public.source add column language text check (language in ('vi', 'en', 'auto'));
alter table public.source add column duration_ms bigint check (duration_ms >= 0);
-- {audio_available, visual_available, captions_available, original_playback_available}
alter table public.source add column capabilities_json jsonb;
-- Độ phủ thực tế: tỉ lệ lời nói, khoảng chưa đọc hình, số đoạn bị loại...
alter table public.source add column coverage_json jsonb;

-- Media giới hạn theo gói Storage: mỗi object ≤ 50 MB → video được tải theo nhiều phần.
insert into storage.buckets (id, name, public, file_size_limit)
values ('media', 'media', false, 52428800)
on conflict (id) do nothing;

-- ----------------------------------------------------------------- job payload

-- Tham số của job reindex/sửa transcript (profile, revision gốc, danh sách sửa).
alter table public.job add column payload_json jsonb not null default '{}'::jsonb;

-- -------------------------------------------------------------- upload session

create table public.upload_session (
  id                 uuid primary key default gen_random_uuid(),
  workspace_id       uuid not null references public.workspace (id) on delete cascade,
  owner_id           uuid not null references auth.users (id) on delete cascade,
  source_id          uuid,
  filename           text not null,
  mime               text not null,
  expected_bytes     bigint not null check (expected_bytes > 0),
  part_bytes         bigint not null check (part_bytes > 0),
  part_count         integer not null check (part_count between 1 and 1000),
  object_prefix      text not null,
  caption_object_key text,
  caption_filename   text,
  language           text not null default 'auto' check (language in ('vi', 'en', 'auto')),
  keep_original      boolean not null default false,
  status             text not null default 'open' check (status in ('open', 'completed', 'cancelled', 'expired')),
  idempotency_key    text,
  request_hash       text,
  expires_at         timestamptz not null,
  created_at         timestamptz not null default now(),
  completed_at       timestamptz,
  unique (id, workspace_id),
  unique (workspace_id, idempotency_key)
);
create index upload_session_open_idx on public.upload_session (status, expires_at) where status = 'open';

-- ----------------------------------------------------------------- acquisition

create table public.acquisition (
  id                uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null,
  source_id         uuid not null,
  connector         text not null check (connector in ('upload', 'youtube', 'drive', 'extension')),
  provider_id       text,
  provider_revision text,
  acquisition_basis text not null,
  acquired_at       timestamptz not null default now(),
  status            text not null default 'pending' check (status in ('pending', 'acquired', 'failed')),
  manifest_json     jsonb not null default '{}'::jsonb,
  manifest_sha256   text,
  unique (id, workspace_id),
  foreign key (source_id, workspace_id) references public.source (id, workspace_id) on delete cascade
);
create index acquisition_source_idx on public.acquisition (source_id);

-- ----------------------------------------------------------------- media asset

create table public.media_asset (
  id                   uuid primary key default gen_random_uuid(),
  workspace_id         uuid not null,
  source_id            uuid not null,
  document_revision_id uuid,
  kind                 text not null check (kind in ('original_part', 'audio', 'frame', 'crop', 'caption')),
  object_key           text not null,
  mime                 text,
  sha256               text,
  byte_size            bigint check (byte_size >= 0),
  parent_asset_id      uuid,
  -- audio: {chunk_index, start_ms, end_ms, sample_rate, codec}; frame: {timestamp_ms, width, height, scale}
  meta_json            jsonb not null default '{}'::jsonb,
  retention            text not null default 'source' check (retention in ('staging', 'source')),
  retain_until         timestamptz,
  status               text not null default 'ready' check (status in ('pending', 'ready', 'deleted')),
  created_at           timestamptz not null default now(),
  unique (id, workspace_id),
  foreign key (source_id, workspace_id) references public.source (id, workspace_id) on delete cascade,
  foreign key (document_revision_id, workspace_id) references public.document_revision (id, workspace_id) on delete cascade,
  foreign key (parent_asset_id, workspace_id) references public.media_asset (id, workspace_id)
);
create index media_asset_source_idx on public.media_asset (source_id, kind);
create index media_asset_expiry_idx on public.media_asset (retain_until) where retain_until is not null and status = 'ready';

-- ------------------------------------------------------------ extraction revision

create table public.extraction_revision (
  id                   uuid primary key default gen_random_uuid(),
  workspace_id         uuid not null,
  source_id            uuid not null,
  document_revision_id uuid not null,
  parent_revision_id   uuid,                 -- revision gốc khi người dùng sửa transcript/vùng chữ
  profile              text not null,
  input_hashes_json    jsonb not null default '{}'::jsonb,
  versions_json        jsonb not null default '{}'::jsonb,   -- ASR/OCR/sampler/glossary/policy version
  coverage_json        jsonb,
  status               text not null default 'running' check (status in ('running', 'succeeded', 'failed', 'superseded')),
  parse_revision_id    uuid,
  created_by           uuid references auth.users (id) on delete set null,
  created_at           timestamptz not null default now(),
  unique (id, workspace_id),
  foreign key (source_id, workspace_id) references public.source (id, workspace_id) on delete cascade,
  foreign key (document_revision_id, workspace_id) references public.document_revision (id, workspace_id) on delete cascade,
  foreign key (parent_revision_id, workspace_id) references public.extraction_revision (id, workspace_id),
  foreign key (parse_revision_id, workspace_id) references public.parse_revision (id, workspace_id)
);
create index extraction_revision_source_idx on public.extraction_revision (source_id, created_at desc);
create unique index extraction_revision_parse_idx on public.extraction_revision (parse_revision_id) where parse_revision_id is not null;

create table public.transcript_segment (
  id                     uuid primary key default gen_random_uuid(),
  workspace_id           uuid not null,
  extraction_revision_id uuid not null,
  track                  text not null,          -- 'asr:<model>' | 'caption:<file>'
  ordinal                integer not null check (ordinal >= 0),
  text                   text not null,
  language               text,
  start_ms               integer not null check (start_ms >= 0),
  end_ms                 integer not null,
  origin                 text not null check (origin in ('human_caption', 'auto_caption', 'asr', 'manual_edit')),
  -- {avg_logprob, no_speech_prob, compression_ratio, flags: [...], reviewed}
  quality_json           jsonb not null default '{}'::jsonb,
  audio_asset_id         uuid,
  check (end_ms > start_ms),
  unique (extraction_revision_id, ordinal),
  unique (id, workspace_id),
  foreign key (extraction_revision_id, workspace_id) references public.extraction_revision (id, workspace_id) on delete cascade,
  foreign key (audio_asset_id, workspace_id) references public.media_asset (id, workspace_id)
);
create index transcript_segment_time_idx on public.transcript_segment (extraction_revision_id, start_ms);

create table public.frame_region (
  id                     uuid primary key default gen_random_uuid(),
  workspace_id           uuid not null,
  extraction_revision_id uuid not null,
  frame_asset_id         uuid not null,
  ordinal                integer not null check (ordinal >= 0),
  timestamp_ms           integer not null check (timestamp_ms >= 0),
  visible_from_ms        integer not null,
  visible_to_ms          integer not null,
  bbox_json              jsonb not null,         -- [x0, y0, x1, y1] theo pixel của frame gốc
  crop_asset_id          uuid,
  text                   text not null,
  method                 text not null check (method in ('ocr', 'vlm', 'manual_edit')),
  confidence             real,
  quality_json           jsonb not null default '{}'::jsonb,
  review_state           text not null default 'unreviewed' check (review_state in ('unreviewed', 'reviewed', 'rejected')),
  check (visible_to_ms >= visible_from_ms),
  unique (extraction_revision_id, ordinal),
  unique (id, workspace_id),
  foreign key (extraction_revision_id, workspace_id) references public.extraction_revision (id, workspace_id) on delete cascade,
  foreign key (frame_asset_id, workspace_id) references public.media_asset (id, workspace_id),
  foreign key (crop_asset_id, workspace_id) references public.media_asset (id, workspace_id)
);
create index frame_region_time_idx on public.frame_region (extraction_revision_id, visible_from_ms);

-- Quan hệ thời gian giữa lời nói và vùng chữ — KHÔNG phải quan hệ hỗ trợ evidence (mục 5.4).
create table public.alignment_edge (
  workspace_id           uuid not null,
  extraction_revision_id uuid not null,
  segment_id             uuid not null,
  region_id              uuid not null,
  relation               text not null check (relation in ('during')),
  score                  real not null,
  policy_version         text not null,
  primary key (segment_id, region_id),
  foreign key (extraction_revision_id, workspace_id) references public.extraction_revision (id, workspace_id) on delete cascade,
  foreign key (segment_id, workspace_id) references public.transcript_segment (id, workspace_id) on delete cascade,
  foreign key (region_id, workspace_id) references public.frame_region (id, workspace_id) on delete cascade
);

-- Canonical span (code point) → segment/region, thời gian, asset, bbox. Nhiều-nhiều (mục 6, bất biến 2).
create table public.source_map_span (
  id                bigint generated always as identity primary key,
  workspace_id      uuid not null,
  parse_revision_id uuid not null,
  char_start        integer not null check (char_start >= 0),
  char_end          integer not null,
  target_type       text not null check (target_type in ('segment', 'region')),
  target_id         uuid not null,
  target_char_start integer not null,
  target_char_end   integer not null,
  start_ms          integer not null,
  end_ms            integer not null,
  asset_id          uuid,
  bbox_json         jsonb,
  precision         text not null check (precision in ('segment', 'region')),
  check (char_end > char_start),
  foreign key (parse_revision_id, workspace_id) references public.parse_revision (id, workspace_id) on delete cascade
);
create index source_map_span_idx on public.source_map_span (parse_revision_id, char_start);

-- ------------------------------------------------------------- node / index / run

alter table public.node add column modality text check (modality in ('text', 'speech', 'caption', 'board'));
alter table public.node add column start_ms integer;
alter table public.node add column end_ms integer;

-- Một parse revision có thể có generation active cho nhiều profile; không trộn embedding khác profile.
alter table public.index_generation add column model_profile text not null default 'product';
drop index public.index_generation_active_idx;
create unique index index_generation_active_idx on public.index_generation (parse_revision_id, model_profile)
  where state = 'active';

alter table public.run drop constraint run_profile_check;
alter table public.run add constraint run_profile_check check (profile in ('product', 'v11_parity', 'lecture_vi_v1'));

alter table public.claim_evidence add column modality text check (modality in ('text', 'speech', 'caption', 'board'));
alter table public.claim_evidence add column locator_json jsonb;
alter table public.claim_evidence add column extraction_revision_id uuid;
-- automatic: chỉ có bản chép máy; reviewed: mọi đoạn được trích đã có người đối chiếu (mục 7).
alter table public.claim_evidence add column transcription_state text not null default 'not_applicable'
  check (transcription_state in ('not_applicable', 'automatic', 'reviewed'));

-- -------------------------------------------------------------------------- RLS

alter table public.upload_session      enable row level security;
alter table public.acquisition         enable row level security;
alter table public.media_asset         enable row level security;
alter table public.extraction_revision enable row level security;
alter table public.transcript_segment  enable row level security;
alter table public.frame_region        enable row level security;
alter table public.alignment_edge      enable row level security;
alter table public.source_map_span     enable row level security;

revoke all on public.upload_session, public.acquisition, public.media_asset, public.extraction_revision,
  public.transcript_segment, public.frame_region, public.alignment_edge, public.source_map_span
from anon, authenticated;

-- Chỉ metadata được đọc qua Data API; nội dung transcript/OCR/media chỉ qua FastAPI.
grant select on public.upload_session, public.acquisition, public.extraction_revision to authenticated;

create policy upload_session_owner_read on public.upload_session
  for select to authenticated using (owner_id = (select auth.uid()) and public.is_workspace_owner(workspace_id));
create policy acquisition_owner_read on public.acquisition
  for select to authenticated using (public.is_workspace_owner(workspace_id));
create policy extraction_revision_owner_read on public.extraction_revision
  for select to authenticated using (public.is_workspace_owner(workspace_id));
