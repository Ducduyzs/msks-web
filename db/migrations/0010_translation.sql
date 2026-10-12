-- Dịch tài liệu (11/10/2026): bản dịch máy theo từng đoạn lá của một parse revision, chỉ để đọc.
-- Trích dẫn và kiểm chứng NLI luôn dựa trên bản gốc; bản dịch không được index hay đưa vào context QA.
--
-- source_translation: một bản dịch cho mỗi (parse revision, ngôn ngữ đích); sửa bản chép / nạp lại tạo revision mới
--   nên bản dịch cũ vẫn gắn đúng văn bản đã dịch.
-- translation_segment: bản dịch từng đoạn lá (node level 'child'); `flags` ghi cảnh báo như số liệu bị đổi.
-- Xóa nguồn → cascade qua source/node. Chỉ đọc qua API (kiểm quyền + tombstone): RLS bật, không có policy.

alter table public.job drop constraint job_kind_check;
alter table public.job add constraint job_kind_check
  check (kind in ('ingest', 'reindex', 'delete', 'run', 'gc', 'translate'));

create table public.source_translation (
  id                uuid primary key default gen_random_uuid(),
  workspace_id      uuid not null references public.workspace (id) on delete cascade,
  source_id         uuid not null,
  parse_revision_id uuid not null,
  target_lang       text not null check (target_lang ~ '^[a-z]{2}(-[A-Za-z]{2,4})?$'),
  status            text not null default 'queued' check (status in ('queued', 'running', 'succeeded', 'failed')),
  progress          real not null default 0 check (progress between 0 and 1),
  segment_count     integer not null default 0 check (segment_count >= 0),
  char_count        integer not null default 0 check (char_count >= 0),
  flagged_count     integer not null default 0 check (flagged_count >= 0),
  model             text,
  error_json        jsonb,
  requested_by      uuid references auth.users (id) on delete set null,
  created_at        timestamptz not null default now(),
  finished_at       timestamptz,
  unique (parse_revision_id, target_lang),
  unique (id, workspace_id),
  foreign key (source_id, workspace_id) references public.source (id, workspace_id) on delete cascade,
  foreign key (parse_revision_id, workspace_id) references public.parse_revision (id, workspace_id) on delete cascade
);
create index source_translation_source_idx on public.source_translation (source_id, created_at desc);

create table public.translation_segment (
  translation_id uuid not null,
  workspace_id   uuid not null,
  node_id        uuid not null,
  ordinal        integer not null check (ordinal >= 0),
  text           text not null,
  flags          jsonb not null default '[]'::jsonb,
  primary key (translation_id, node_id),
  foreign key (translation_id, workspace_id) references public.source_translation (id, workspace_id) on delete cascade,
  foreign key (node_id, workspace_id) references public.node (id, workspace_id) on delete cascade
);
create index translation_segment_order_idx on public.translation_segment (translation_id, ordinal);

alter table public.source_translation  enable row level security;
alter table public.translation_segment enable row level security;
revoke all on public.source_translation  from anon, authenticated;
revoke all on public.translation_segment from anon, authenticated;
