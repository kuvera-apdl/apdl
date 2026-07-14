import { QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { RouterProvider } from 'react-router-dom'

import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { AuthProvider } from '@/core/auth'
import { LiveProvider } from '@/core/live'
import { createQueryClient } from '@/core/queryClient'
import { ThemeProvider } from '@/core/theme'
import { WorkspaceProvider } from '@/core/workspace'
import { createRouter } from '@/router'

export function App() {
  const [queryClient] = useState(createQueryClient)
  const [router] = useState(createRouter)

  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <WorkspaceProvider>
            <LiveProvider>
              <TooltipProvider delayDuration={300}>
                <RouterProvider router={router} future={{ v7_startTransition: true }} />
              </TooltipProvider>
              <Toaster />
            </LiveProvider>
          </WorkspaceProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  )
}
