import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FolderOpen, Plus } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api'
import { EmptyState, ErrorBox, Loading, Spinner } from '../components/ui'
import { fmtDate } from '../lib/labels'

export function WorkspacesPage() {
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: () => api.listWorkspaces() })
  const [name, setName] = useState('')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const create = useMutation({
    mutationFn: (value: string) => api.createWorkspace(value),
    onSuccess: (workspace) => {
      queryClient.invalidateQueries({ queryKey: ['workspaces'] })
      navigate(`/w/${workspace.id}/sources`)
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    if (name.trim()) create.mutate(name.trim())
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 px-4 py-10">
      <div>
        <h1 className="text-2xl font-semibold">Không gian làm việc</h1>
        <p className="mt-1 text-muted">
          Mỗi workspace là một tập nguồn. Câu hỏi và báo cáo tổng hợp chỉ dùng nguồn trong workspace đó.
        </p>
      </div>

      <form onSubmit={submit} className="card flex flex-col gap-2 p-4 sm:flex-row">
        <input
          className="input"
          placeholder="Tên workspace mới, ví dụ: Tổng quan RAG cho tài liệu khoa học"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={120}
        />
        <button className="btn btn-primary shrink-0" disabled={!name.trim() || create.isPending}>
          {create.isPending ? <Spinner /> : <Plus className="size-4" />} Tạo workspace
        </button>
      </form>
      {create.error && <ErrorBox error={create.error} />}

      {workspaces.isPending && <Loading />}
      {workspaces.error && <ErrorBox error={workspaces.error} />}
      {workspaces.data?.length === 0 && (
        <EmptyState icon={<FolderOpen className="size-8" />} title="Chưa có workspace nào">
          Tạo một workspace rồi thêm tài liệu để bắt đầu.
        </EmptyState>
      )}
      <ul className="grid gap-3 sm:grid-cols-2">
        {workspaces.data?.map((w) => (
          <li key={w.id}>
            <Link to={`/w/${w.id}/sources`} className="card block p-4 transition-shadow hover:shadow-md">
              <p className="font-medium">{w.name}</p>
              <p className="mt-2 text-sm text-muted">
                {w.source_count} nguồn · {w.run_count} lượt chạy · tạo {fmtDate(w.created_at)}
              </p>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  )
}
