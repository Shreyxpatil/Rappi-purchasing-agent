// Typed client for the FastAPI backend (see backend/app/api.py).

export type Provider = 'scripted' | 'gemini' | 'openai_compat'

export interface Scenario {
  id: string
  scenario: string
  title: string
  description: string
  trigger: { type: string; node: string; sku: string; recommendation_id?: string; po_id?: string; message: string }
  primary_supplier: string | null
  expected: { outcome: string; qty_min: number; qty_max: number }
  scripted: boolean
}

export interface Step {
  seq: number
  state: string
  kind: string
  name: string
  input: Record<string, unknown>
  output: Record<string, unknown>
  ok: boolean
  latency_ms: number
  tokens_in: number | null
  tokens_out: number | null
}

export interface Factor { name: string; value: string; effect: string }
export interface Check { name: string; status: string; detail: Record<string, unknown> }

export interface Decision {
  outcome: string
  quantity: number
  option_id: string | null
  option_kind: string | null
  supplier_id: string | null
  value: number
  confidence: string
  factors: Factor[]
  constraints_checked: Check[]
  recommendation_check: Check[] | null
  residual_risk: Record<string, number> | null
  information_needed: string[]
  reasons?: string[]
}

export interface Alternative {
  option_id: string
  label: string
  qty: number
  value: number
  over_budget: number
  stockout_day: number | null
  unmet_units: number
  tradeoffs: string[]
}

export interface Approval {
  id: number
  run_id: number
  status: string
  reasons: string[]
  summary: string
  alternatives: Alternative[]
  requested_at: string
  decided_by: string | null
  comment: string | null
}

export interface RunSummary {
  id: number
  scenario_id: string
  provider: string
  status: string
  reason: string | null // why it ended FAILED / ESCALATED, e.g. LLM_QUOTA_EXHAUSTED
  state: string
  outcome: string | null
  quantity: number | null
  replans: number
  started_at: string
}

export interface Projection {
  safety_stock: number | null
  do_nothing: number[] | null
  chosen: number[] | null
  chosen_label: string | null
  confirmed: number[] | null
}

export interface RunDetail extends RunSummary {
  decision: Decision | null
  narrative: string | null
  narrative_source: string | null
  error: string | null
  steps: Step[]
  approvals: Approval[]
  projection: Projection | null
}

export interface POLine { sku: string; qty_ordered: number; qty_confirmed: number | null; unit_cost: number; expected_arrival: string; status: string }
export interface POEvent { at: string; type: string; source: string; payload: Record<string, unknown> }
export interface PurchaseOrder { id: string; node: string; supplier: string; status: string; currency: string; created_by: string; run_id: number | null; lines: POLine[]; events: POEvent[] }
export interface Transfer { id: string; from: string; to: string; sku: string; qty: number; expected_arrival: string; status: string }

export interface EvalRun {
  case: string
  scenario: string
  provider: string
  run: number
  passed: boolean
  dimensions: Record<string, boolean | null>
  extras: Record<string, boolean>
  failures: Record<string, string>
  status?: string
  outcome?: string
  quantity?: number
  duration_s: number
  llm_calls?: number
  error?: string
  judge?: { average?: number; comment?: string }
}
export interface EvalResults { provider: string; model: string; updated_at: string; runs: EvalRun[] }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, { headers: { 'content-type': 'application/json' }, ...init })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? res.statusText))
  }
  return res.json() as Promise<T>
}

export const api = {
  scenarios: () => call<Scenario[]>('/scenarios'),
  runs: () => call<RunSummary[]>('/runs'),
  run: (id: number) => call<RunDetail>(`/runs/${id}`),
  startRun: (body: { scenario_id: string; provider: Provider; supplier_behaviour?: Record<string, unknown[]> }) =>
    call<{ run_id: number }>('/runs', { method: 'POST', body: JSON.stringify(body) }),
  approvals: (status = 'PENDING') => call<Approval[]>(`/approvals?status=${status}`),
  answer: (id: number, approve: boolean, comment: string) =>
    call<{ run_id: number }>(`/approvals/${id}`, {
      method: 'POST',
      body: JSON.stringify({ approve, comment, decided_by: 'buyer (UI)' }),
    }),
  purchaseOrders: () => call<{ scenario_id: string | null; purchase_orders: PurchaseOrder[]; transfers: Transfer[] }>('/purchase-orders'),
  evals: () => call<EvalResults[]>('/evals'),
}
