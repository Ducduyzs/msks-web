-- MSKS — phân quyền (ARCHITECTURE.md mục 15: "server xác định scope từ phiên đã xác thực").
--
-- Mô hình:
--   * anon: không truy cập bảng nào.
--   * authenticated: chỉ SELECT dữ liệu thuộc workspace mình sở hữu (phòng thủ nhiều lớp nếu
--     client đọc qua Supabase Data API). Mọi thao tác ghi đi qua backend FastAPI.
--   * Backend dùng kết nối Postgres trực tiếp hoặc secret key → bỏ qua RLS.
--   * Nội dung của nguồn đã tombstone bị ẩn khỏi người dùng (mục 6.5); backend trả tham chiếu đã che.

-- Bật RLS cho mọi bảng nghiệp vụ.
alter table public.workspace         enable row level security;
alter table public.source            enable row level security;
alter table public.source_relation   enable row level security;
alter table public.document_revision enable row level security;
alter table public.parse_revision    enable row level security;
alter table public.index_generation  enable row level security;
alter table public.node              enable row level security;
alter table public.node_edge         enable row level security;
alter table public.run               enable row level security;
alter table public.context_block     enable row level security;
alter table public.claim             enable row level security;
alter table public.claim_evidence    enable row level security;
alter table public.report_section    enable row level security;
alter table public.job               enable row level security;
alter table public.outbox_event      enable row level security;
alter table public.run_event         enable row level security;
alter table public.job_event         enable row level security;

-- Thu hồi quyền mặc định Supabase cấp cho anon/authenticated trên schema public.
revoke all on all tables in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
revoke all on all functions in schema public from public, anon, authenticated;
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from public, anon, authenticated;

-- Chỉ cấp SELECT cho authenticated trên bảng người dùng cần đọc. outbox_event/job_event là nội bộ.
grant select on public.workspace, public.source, public.source_relation, public.document_revision,
  public.parse_revision, public.index_generation, public.node, public.node_edge, public.run,
  public.context_block, public.claim, public.claim_evidence, public.report_section, public.job,
  public.run_event
to authenticated;

-- ------------------------------------------------------------------ helpers

create function public.is_workspace_owner(p_workspace_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.workspace w
    where w.id = p_workspace_id and w.owner_id = (select auth.uid())
  );
$$;

-- Node có thuộc nguồn đã tombstone không (node → parse_revision → document_revision → source).
create function public.node_source_deleted(p_node_id uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select exists (
    select 1
    from public.node n
    join public.parse_revision pr on pr.id = n.parse_revision_id
    join public.document_revision dr on dr.id = pr.document_revision_id
    join public.source s on s.id = dr.source_id
    where n.id = p_node_id and s.deleted_at is not null
  );
$$;

grant execute on function public.is_workspace_owner(uuid) to authenticated;
grant execute on function public.node_source_deleted(uuid) to authenticated;

-- ----------------------------------------------------------------- policies

create policy workspace_owner_read on public.workspace
  for select to authenticated using (owner_id = (select auth.uid()));

create policy source_owner_read on public.source
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy source_relation_owner_read on public.source_relation
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy document_revision_owner_read on public.document_revision
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy parse_revision_owner_read on public.parse_revision
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy index_generation_owner_read on public.index_generation
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy node_owner_read on public.node
  for select to authenticated
  using (public.is_workspace_owner(workspace_id) and not public.node_source_deleted(id));

create policy node_edge_owner_read on public.node_edge
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy run_owner_read on public.run
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy context_block_owner_read on public.context_block
  for select to authenticated
  using (public.is_workspace_owner(workspace_id) and not public.node_source_deleted(node_id));

create policy claim_owner_read on public.claim
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy claim_evidence_owner_read on public.claim_evidence
  for select to authenticated
  using (public.is_workspace_owner(workspace_id) and not public.node_source_deleted(node_id));

create policy report_section_owner_read on public.report_section
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy job_owner_read on public.job
  for select to authenticated using (public.is_workspace_owner(workspace_id));

create policy run_event_owner_read on public.run_event
  for select to authenticated using (public.is_workspace_owner(workspace_id));
