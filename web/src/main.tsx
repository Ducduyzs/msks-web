import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { ApiError } from './api'
import { AuthGate } from './auth/AuthGate'
import { LanguageRoot } from './components/PreferencesMenu'
import { initialLanguage, setLanguage } from './i18n'
import { applyPreferences } from './lib/preferences'
import { router } from './router'
import './index.css'

const queryClient: QueryClient = new QueryClient({
  // Phiên hết hạn giữa chừng (401) → kiểm tra lại phiên để AuthGate hiện màn hình đăng nhập.
  queryCache: new QueryCache({
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 1,
    },
  },
})

// Áp chế độ sáng/tối + màu trước khi render (không chớp màu); nạp từ điển ngôn ngữ đã chọn rồi mới vẽ giao diện.
applyPreferences()
void setLanguage(initialLanguage()).finally(() =>
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <LanguageRoot>
          <AuthGate>
            <RouterProvider router={router} />
          </AuthGate>
        </LanguageRoot>
      </QueryClientProvider>
    </StrictMode>,
  ),
)
