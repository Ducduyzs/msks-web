-- Raw SSE payloads contain evidence snapshots. Read them through FastAPI,
-- which rehydrates evidence against current tombstones, never via Data API.
revoke select on public.run_event from authenticated;

-- canonical_text was added in 0004; the old owner-only policy exposed it
-- while a source was tombstoned and its asynchronous GC had not finished.
alter policy parse_revision_owner_read on public.parse_revision
  using (
    public.is_workspace_owner(workspace_id)
    and exists (
      select 1 from public.document_revision d
      join public.source s on s.id = d.source_id
      where d.id = parse_revision.document_revision_id
        and s.deleted_at is null
    )
  );
