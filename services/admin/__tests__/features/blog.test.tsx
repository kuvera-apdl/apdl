import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, test } from 'vitest'

import { BlogPage } from '../../src/features/blog/BlogPage'
import { JavaScriptSdkArticlePage } from '../../src/features/blog/JavaScriptSdkArticlePage'
import { PythonSdkArticlePage } from '../../src/features/blog/PythonSdkArticlePage'

function renderPage(page: React.ReactNode) {
  return render(<MemoryRouter>{page}</MemoryRouter>)
}

describe('SDK blog', () => {
  test('lists both SDK setup articles', () => {
    renderPage(<BlogPage />)

    expect(screen.getByRole('heading', { name: 'Blog' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /how to set up the javascript sdk/i })).toHaveAttribute(
      'href',
      '/blog/javascript-sdk',
    )
    expect(screen.getByRole('link', { name: /how to set up the python sdk/i })).toHaveAttribute(
      'href',
      '/blog/python-sdk',
    )
  })

  test('documents the canonical JavaScript browser setup', () => {
    renderPage(<JavaScriptSdkArticlePage />)

    expect(
      screen.getByRole('heading', { name: 'How to set up the JavaScript SDK' }),
    ).toBeInTheDocument()
    expect(screen.getByText('npm install @apdl-oss/sdk')).toBeInTheDocument()
    expect(screen.getAllByText(/client_yourproject_replace_me/)).toHaveLength(2)
    expect(screen.getByRole('link', { name: 'Verify integration' })).toHaveAttribute(
      'href',
      '/settings/verify',
    )
  })

  test('documents the canonical Python server setup', () => {
    renderPage(<PythonSdkArticlePage />)

    expect(
      screen.getByRole('heading', { name: 'How to set up the Python SDK' }),
    ).toBeInTheDocument()
    expect(screen.getByText('uv add apdl-sdk')).toBeInTheDocument()
    expect(screen.getByText(/proj_yourproject_replace_me/)).toBeInTheDocument()
    expect(screen.getByText(/Store it in your secret manager/i)).toBeInTheDocument()
  })
})
