import { Navigate, createBrowserRouter, type RouteObject } from 'react-router-dom'
import { NotFound, RouteError, Shell } from './components/Shell'
import { QaPage } from './pages/QaPage'
import { RunDetailPage } from './pages/RunDetailPage'
import { RunsPage } from './pages/RunsPage'
import { SourcesPage } from './pages/SourcesPage'
import { SynthesisPage } from './pages/SynthesisPage'
import { WorkspaceLayout } from './pages/WorkspaceLayout'
import { WorkspacesPage } from './pages/WorkspacesPage'

export const routes: RouteObject[] = [
  {
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <WorkspacesPage /> },
      {
        path: 'w/:workspaceId',
        element: <WorkspaceLayout />,
        children: [
          { index: true, element: <Navigate to="sources" replace /> },
          { path: 'sources', element: <SourcesPage /> },
          { path: 'qa', element: <QaPage /> },
          { path: 'synthesis', element: <SynthesisPage /> },
          { path: 'runs', element: <RunsPage /> },
          { path: 'runs/:runId', element: <RunDetailPage /> },
        ],
      },
      { path: '*', element: <NotFound /> },
    ],
  },
]

export const router = createBrowserRouter(routes)
