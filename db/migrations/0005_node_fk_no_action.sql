-- Khóa ngoại tới node: RESTRICT kiểm tra ngay từng dòng nên xóa cascade cả workspace có thể thất bại
-- tùy thứ tự. NO ACTION kiểm tra cuối câu lệnh: xóa riêng node đang được trích vẫn bị chặn,
-- còn xóa workspace (cascade cả run lẫn node) thì hợp lệ.
alter table public.context_block drop constraint context_block_node_id_workspace_id_fkey;
alter table public.context_block add constraint context_block_node_id_workspace_id_fkey
  foreign key (node_id, workspace_id) references public.node (id, workspace_id);

alter table public.claim_evidence drop constraint claim_evidence_node_id_workspace_id_fkey;
alter table public.claim_evidence add constraint claim_evidence_node_id_workspace_id_fkey
  foreign key (node_id, workspace_id) references public.node (id, workspace_id);
