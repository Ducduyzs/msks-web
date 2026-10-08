-- MSKS — kho file gốc trên Supabase Storage, làm adapter BlobStore (mục 12).
-- Bucket riêng tư, không có policy cho anon/authenticated: chỉ backend (secret key) đọc/ghi.
-- Object key do server tạo: <workspace_id>/<source_id>/<document_revision_id>.
insert into storage.buckets (id, name, public, file_size_limit)
values ('documents', 'documents', false, 52428800)
on conflict (id) do nothing;
