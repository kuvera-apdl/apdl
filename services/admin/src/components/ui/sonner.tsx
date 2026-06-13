import { Toaster as Sonner } from 'sonner'

import { useTheme } from '@/core/theme'

function Toaster() {
  const { theme } = useTheme()
  return (
    <Sonner
      theme={theme}
      position="bottom-right"
      toastOptions={{
        classNames: {
          toast:
            'group rounded-lg border bg-background text-foreground shadow-lg',
          description: 'text-muted-foreground',
        },
      }}
    />
  )
}

export { Toaster }
