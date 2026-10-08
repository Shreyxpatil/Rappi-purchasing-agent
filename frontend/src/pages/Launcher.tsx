import { useEffect, useState } from 'react'
import { api, type Provider, type RunSummary, type Scenario } from '../api'
import { Card, RunStatus } from '../ui'

const GROUPS: Record<string, string> = {
  S1: 'S1 · Recommendation review',
  S2: 'S2 · Supplier cannot fulfil',
  S3: 'S3 · Demand changed',
  S4: 'S4 · Purchasing constraint',
  X: 'Recovery & safety',
}

const INJECTIONS: Record<string, unknown[] | null> = {
  none: null,
  'Supplier rejects': [{ type: 'REJECTED', message: 'injected from the UI' }],
  'Supplier delays 2 days': [{ type: 'DELAYED', days: 2, message: 'injected from the UI' }],
  'Price +8%': [{ type: 'PRICE_CHANGE', pct: 8, message: 'injected from the UI' }],
}

export default function Launcher({ onOpenRun }: { onOpenRun: (id: number) => void }) {
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [provider, setProvider] = useState<Provider>('scripted')
  const [injection, setInjection] = useState('none')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')

  useEffect(() => {
    api.scenarios().then(setScenarios).catch((e) => setError(String(e)))
    api.runs().then(setRuns).catch(() => {})
  }, [])

  async function launch(s: Scenario) {
    setBusy(s.id)
    setError('')
    try {
      const behaviour = INJECTIONS[injection]
      const { run_id } = await api.startRun({
        scenario_id: s.id,
        provider,
        ...(behaviour && s.primary_supplier ? { supplier_behaviour: { [s.primary_supplier]: behaviour } } : {}),
      })
      onOpenRun(run_id)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
      <div className="space-y-4">
        <Card title="Run a scenario">
          <div className="flex flex-wrap items-end gap-4 text-sm">
            <label className="flex flex-col gap-1">
              <span className="text-slate-500">Provider</span>
              <select className="rounded border px-2 py-1" value={provider} onChange={(e) => setProvider(e.target.value as Provider)}>
                <option value="scripted">scripted (offline, ~1 s)</option>
                <option value="gemini">Gemini (~2–3 min)</option>
                <option value="openai_compat">Groq (~4 min on free tier)</option>
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-slate-500">Inject a supplier failure</span>
              <select className="rounded border px-2 py-1" value={injection} onChange={(e) => setInjection(e.target.value)}>
                {Object.keys(INJECTIONS).map((k) => <option key={k}>{k}</option>)}
              </select>
            </label>
            {injection !== 'none' && provider === 'scripted' && (
              <p className="max-w-xs text-xs text-amber-700">
                A scripted trajectory cannot adapt to a failure it was not written for: use Gemini/Groq, or the
                x_* scenarios which script their own failures.
              </p>
            )}
          </div>
          {error && <p className="mt-3 rounded bg-rose-50 p-2 text-sm text-rose-700">{error}</p>}
        </Card>

        {Object.entries(GROUPS).map(([key, label]) => (
          <Card key={key} title={label}>
            <ul className="divide-y divide-slate-100">
              {scenarios.filter((s) => s.scenario === key).map((s) => (
                <li key={s.id} className="flex items-start justify-between gap-4 py-3">
                  <div>
                    <div className="font-medium text-slate-900">{s.title}</div>
                    <div className="mt-0.5 text-sm text-slate-500">{s.description}</div>
                    <div className="mt-1 flex gap-2 text-xs text-slate-400">
                      <code>{s.id}</code>
                      <span>expected: {s.expected.outcome} {s.expected.qty_min === s.expected.qty_max ? s.expected.qty_min : `${s.expected.qty_min}–${s.expected.qty_max}`}</span>
                    </div>
                  </div>
                  <button
                    disabled={!!busy}
                    onClick={() => launch(s)}
                    className="shrink-0 rounded bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
                  >
                    {busy === s.id ? 'Starting…' : 'Run'}
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        ))}
      </div>

      <Card title="Recent runs">
        <ul className="space-y-2 text-sm">
          {runs.slice(0, 15).map((r) => (
            <li key={r.id}>
              <button onClick={() => onOpenRun(r.id)} className="w-full rounded p-2 text-left hover:bg-slate-50">
                <div className="flex justify-between gap-2">
                  <span className="font-mono text-xs">#{r.id} {r.scenario_id}</span>
                  <RunStatus status={r.status} reason={r.reason} />
                </div>
                <div className="text-xs text-slate-500">{r.provider} · {r.outcome ?? '…'} {r.quantity ?? ''}</div>
              </button>
            </li>
          ))}
          {runs.length === 0 && <li className="text-slate-400">No runs yet.</li>}
        </ul>
      </Card>
    </div>
  )
}
