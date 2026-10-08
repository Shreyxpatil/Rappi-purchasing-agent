import { useEffect, useState } from 'react'
import { api, type Approval } from '../api'
import { Badge, Card, fmt, statusTone } from '../ui'

export default function Approvals({ onOpenRun }: { onOpenRun: (id: number) => void }) {
  const [pending, setPending] = useState<Approval[]>([])
  const [decided, setDecided] = useState<Approval[]>([])
  const [comment, setComment] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState('')

  const load = () => {
    api.approvals('PENDING').then(setPending).catch((e) => setError(String(e)))
    api.approvals('').then((all) => setDecided(all.filter((a) => a.status !== 'PENDING').slice(0, 10)))
  }
  useEffect(load, [])

  async function answer(a: Approval, approve: boolean) {
    setBusy(a.id)
    try {
      await api.answer(a.id, approve, comment[a.id] ?? '')
      load()
      onOpenRun(a.run_id) // the run resumes in the background; follow it live
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-4">
      {error && <p className="rounded bg-rose-50 p-2 text-sm text-rose-700">{error}</p>}
      <Card title={`Pending approvals (${pending.length})`} right={<button onClick={load} className="text-sm text-slate-500 hover:underline">refresh</button>}>
        {pending.length === 0 && <p className="text-sm text-slate-400">Nothing waiting. Run s4_budget_binding or s2_partial_needs_alternate to see one.</p>}
        <div className="space-y-4">
          {pending.map((a) => (
            <div key={a.id} className="rounded border border-amber-200 bg-amber-50/40 p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{a.summary}</span>
                {a.reasons.map((r) => <Badge key={r} tone="amber">{r}</Badge>)}
                <button onClick={() => onOpenRun(a.run_id)} className="ml-auto text-xs text-slate-500 hover:underline">run #{a.run_id}</button>
              </div>
              {a.alternatives.length > 0 && (
                <table className="mt-3 w-full text-sm">
                  <thead className="text-left text-xs text-slate-500">
                    <tr><th></th><th>Option</th><th>Qty</th><th>Value</th><th>Over budget</th><th>Stockout</th><th>Trade-offs</th></tr>
                  </thead>
                  <tbody>
                    {a.alternatives.map((alt, i) => (
                      <tr key={alt.option_id} className="border-t border-amber-100 align-top">
                        <td className="py-1 pr-2 text-xs text-slate-500">{i === 0 ? 'requested' : 'if refused'}</td>
                        <td className="py-1 pr-2">{alt.label}</td>
                        <td className="py-1 pr-2">{fmt(alt.qty)}</td>
                        <td className="py-1 pr-2">{fmt(alt.value)}</td>
                        <td className="py-1 pr-2">{alt.over_budget ? fmt(alt.over_budget) : '–'}</td>
                        <td className="py-1 pr-2">{alt.stockout_day != null ? `day ${alt.stockout_day} (${fmt(alt.unmet_units)} unmet)` : 'none'}</td>
                        <td className="py-1 text-xs text-slate-500">{alt.tradeoffs.join('; ') || '–'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <input
                  placeholder="comment (optional)"
                  className="min-w-64 flex-1 rounded border px-2 py-1 text-sm"
                  value={comment[a.id] ?? ''}
                  onChange={(e) => setComment({ ...comment, [a.id]: e.target.value })}
                />
                <button disabled={busy === a.id} onClick={() => answer(a, true)} className="rounded bg-emerald-600 px-3 py-1 text-sm text-white disabled:opacity-50">Approve</button>
                <button disabled={busy === a.id} onClick={() => answer(a, false)} className="rounded bg-rose-600 px-3 py-1 text-sm text-white disabled:opacity-50">Reject</button>
              </div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Recently decided">
        <ul className="space-y-1 text-sm">
          {decided.map((a) => (
            <li key={a.id} className="flex flex-wrap items-center gap-2">
              <Badge tone={statusTone(a.status)}>{a.status}</Badge>
              <span>{a.summary}</span>
              <span className="text-xs text-slate-400">{a.decided_by}{a.comment ? `: “${a.comment}”` : ''}</span>
            </li>
          ))}
          {decided.length === 0 && <li className="text-slate-400">None yet.</li>}
        </ul>
      </Card>
    </div>
  )
}
