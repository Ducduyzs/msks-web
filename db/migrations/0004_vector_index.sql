-- MSKS — index vector trên pgvector (thay Qdrant ở MVP vì môi trường chưa chạy được Docker).
-- Giữ ngữ nghĩa mục 6.5: vector ghi vào index_generation mới; query chỉ đọc generation 'active'
-- nằm trong snapshot của run. Một workspace tối đa ~20.000 leaf nên quét chính xác theo generation
-- (không dùng HNSW để tránh mất recall khi lọc theo generation/ACL).

create extension if not exists vector with schema extensions;

create table public.leaf_embedding (
  workspace_id        uuid not null,
  index_generation_id uuid not null,
  node_id             uuid not null,
  dense               extensions.vector(1024) not null,      -- BGE-M3 dense, đã chuẩn hóa
  sparse              extensions.sparsevec(250002) not null, -- BGE-M3 lexical weights
  sbert               extensions.vector(768) not null,       -- multi-qa-mpnet-base-cos-v1, cho Agreement Ranking
  primary key (index_generation_id, node_id),
  foreign key (index_generation_id, workspace_id)
    references public.index_generation (id, workspace_id) on delete cascade,
  foreign key (node_id, workspace_id) references public.node (id, workspace_id) on delete cascade
);
create index leaf_embedding_node_idx on public.leaf_embedding (node_id);

alter table public.leaf_embedding enable row level security;
revoke all on public.leaf_embedding from anon, authenticated;

-- Trạng thái cuối của nguồn sau khi job xóa hoàn tất (dữ liệu đã thu gom).
alter table public.source drop constraint source_status_check;
alter table public.source add constraint source_status_check
  check (status in ('uploaded', 'parsing', 'chunking', 'embedding', 'publishing',
                    'ready', 'failed', 'cancelled', 'deleting', 'deleted'));
alter table public.source drop constraint source_check;
alter table public.source add constraint source_tombstone_check
  check (status not in ('deleting', 'deleted') or deleted_at is not null);

-- Canonical text và ranh giới trang của parse revision (mục 5.4), lưu cạnh node để trình xem đọc nhanh.
alter table public.parse_revision add column canonical_text text;
alter table public.parse_revision add column pages_json jsonb;   -- [{page, char_start, char_end}] hoặc null
