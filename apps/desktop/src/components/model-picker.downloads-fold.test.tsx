import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $localModelsEnabled } from '@/store/local-models-flag'
import { $localRuntimeJobs } from '@/store/local-runtime-jobs'
import { stubResizeObserver } from '@/test/jsdom'
import type { LocalRuntimeJob } from '@/types/hermes'

import { ModelPickerDialog } from './model-picker'

stubResizeObserver()

vi.mock('@/hermes', () => ({
  getLocalModelsStatus: vi.fn().mockResolvedValue({ loading: {} })
}))
vi.mock('@/lib/model-options', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestModelOptions: vi.fn().mockResolvedValue({
    providers: [
      {
        authenticated: true,
        models: ['qwen3.8-flash-next'],
        name: 'Local',
        slug: 'llamacpp'
      }
    ]
  })
}))

const DOWNLOAD_JOB: LocalRuntimeJob = {
  job_id: 'dl1',
  kind: 'model-download',
  target: 'Qwen3.8 Flash Next (UD-Q4_K_XL)',
  model_id: 'qwen3.8-flash-next',
  status: 'running',
  phase: 'downloading',
  detail: '',
  total_bytes: 100,
  done_bytes: 41,
  percent: 41,
  error: null
}

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const element: ReactElement = (
    <QueryClientProvider client={client}>
      <I18nProvider>
        <ModelPickerDialog
          currentModel="qwen3.8-flash-next"
          currentProvider="llamacpp"
          onOpenChange={() => undefined}
          onSelect={() => undefined}
          open
        />
      </I18nProvider>
    </QueryClientProvider>
  )

  return render(element)
}

beforeEach(() => {
  $localRuntimeJobs.set([DOWNLOAD_JOB])
  // These suites exercise the local-models rows, which ship behind --local.
  $localModelsEnabled.set(true)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  $localRuntimeJobs.set([])
})

// The picker's own download-target filter must obey the same separator fold
// as every other model-search filter in the PR: a hyphen-style query against
// a space-separated (and quant-suffixed) target previously matched nothing.
describe('ModelPickerDialog downloads filter: separator fold', () => {
  it('HYPHEN query matches the SPACED download target (the straggler bug — fails pre-fix)', async () => {
    renderPicker()
    expect(await screen.findByText('Qwen3.8 Flash Next (UD-Q4_K_XL)')).toBeTruthy()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'qwen3.8-flash-next' } })

    await vi.waitFor(() => {
      expect(screen.getByText('Qwen3.8 Flash Next (UD-Q4_K_XL)')).toBeTruthy()
    })
  })

  it('SPACE query also matches, and a non-matching query hides the row', async () => {
    renderPicker()
    expect(await screen.findByText('Qwen3.8 Flash Next (UD-Q4_K_XL)')).toBeTruthy()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'qwen3 8 flash next' } })

    await vi.waitFor(() => {
      expect(screen.getByText('Qwen3.8 Flash Next (UD-Q4_K_XL)')).toBeTruthy()
    })

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzz' } })

    await vi.waitFor(() => {
      expect(screen.queryByText('Qwen3.8 Flash Next (UD-Q4_K_XL)')).toBeNull()
    })
  })
})
