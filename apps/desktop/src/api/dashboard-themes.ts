import type { DashboardThemesResponse } from '@/types/hermes'

import { hermesApi, profileScoped } from './client'

/** Available dashboard themes + the active one (`dashboard.theme` in
 *  config.yaml) — the same active-theme key the web Dashboard reads, so a
 *  name shared between the two surfaces can be synced across them. */
export function getDashboardThemes(): Promise<DashboardThemesResponse> {
  return hermesApi<DashboardThemesResponse>({
    ...profileScoped(),
    path: '/api/dashboard/themes'
  })
}

/** Set the active dashboard theme (persists to config.yaml -> dashboard.theme). */
export function setDashboardTheme(name: string): Promise<{ ok: boolean; theme: string }> {
  return hermesApi<{ ok: boolean; theme: string }>({
    ...profileScoped(),
    path: '/api/dashboard/theme',
    method: 'PUT',
    body: { name }
  })
}
