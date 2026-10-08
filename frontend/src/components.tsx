import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { Check, Decision, Projection } from './api'
import { Badge, Card, fmt, statusTone } from './ui'

const OUTCOME_TONE: Record<string, 'green' | 'amber' | 'red' | 'blue'> = {
  ACCEPT: 'green', MODIFY: 'blue', REJECT: 'red', INVESTIGATE: 'amber',
}

export function DecisionCard({ decision, narrative, source }: { decision: Decision; narrative: string | null; source: string | null }) {
  return (
    <Card title="Decision" right={<Badge tone={decision.confidence === 'high' ? 'green' : decision.confidence === 'low' ? 'red' : 'amber'}>confidence {decision.confidence}</Badge>}>
      <div className="flex flex-wrap items-baseline gap-3">
        <Badge tone={OUTCOME_TONE[decision.outcome] ?? 'gray'}>{decision.outcome}</Badge>
        <span className="text-2xl font-semibold">{fmt(decision.quantity)} units</span>
        {decision.option_id && <code className="text-xs text-slate-500">{decision.option_id}</code>}
        {decision.value > 0 && <span className="text-sm text-slate-500">value {fmt(decision.value)}</span>}
      </div>

      {narrative && (
        <div className="mt-3 rounded bg-slate-50 p-3 text-sm whitespace-pre-line">
          {narrative}
          <div className="mt-1 text-xs text-slate-400">explanation source: {source ?? '–'} (every number checked against the decision)</div>
        </div>
      )}

      <h3 className="mt-4 mb-1 text-sm font-semibold">Factors</h3>
      <table className="w-full text-sm">
        <tbody>
          {decision.factors.map((f) => (
            <tr key={f.name} className="border-t border-slate-100">
              <td className="py-1 pr-2 text-slate-500">{f.name}</td>
              <td className="py-1 pr-2">{f.value}</td>
              <td className="py-1 text-xs text-slate-500">{f.effect}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {decision.constraints_checked.length > 0 && <Checks title="Constraints checked (chosen option)" checks={decision.constraints_checked} />}
      {decision.recommendation_check && <Checks title="Recommendation as is" checks={decision.recommendation_check} />}

      {decision.residual_risk && (
        <p className="mt-3 rounded bg-amber-50 p-2 text-sm text-amber-800">
          Residual risk: {Object.entries(decision.residual_risk).map(([k, v]) => `${k.replaceAll('_', ' ')} ${v}`).join(', ')}
        </p>
      )}
      {decision.information_needed.length > 0 && (
        <p className="mt-3 rounded bg-amber-50 p-2 text-sm text-amber-800">Information needed: {decision.information_needed.join('; ')}</p>
      )}
    </Card>
  )
}

function Checks({ title, checks }: { title: string; checks: Check[] }) {
  return (
    <>
      <h3 className="mt-4 mb-1 text-sm font-semibold">{title}</h3>
      <div className="flex flex-wrap gap-1">
        {checks.filter((c) => c.status !== 'n/a').map((c) => (
          <span key={c.name} title={JSON.stringify(c.detail)}>
            <Badge tone={statusTone(c.status)}>{c.name}: {c.status}</Badge>
          </span>
        ))}
      </div>
    </>
  )
}

export function ProjectionChart({ projection }: { projection: Projection }) {
  const n = Math.max(projection.do_nothing?.length ?? 0, projection.chosen?.length ?? 0, projection.confirmed?.length ?? 0)
  const data = Array.from({ length: n }, (_, d) => ({
    day: `day ${d}`,
    'do nothing': projection.do_nothing?.[d],
    chosen: projection.chosen?.[d],
    confirmed: projection.confirmed?.[d],
  }))
  return (
    <Card title="Inventory projection (end of day)" right={<span className="text-xs text-slate-400">{projection.chosen_label}</span>}>
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
            <XAxis dataKey="day" fontSize={12} />
            <YAxis fontSize={12} />
            <Tooltip />
            <Legend />
            <ReferenceLine y={0} stroke="#e11d48" label={{ value: 'stockout', fontSize: 11, fill: '#e11d48' }} />
            {projection.safety_stock != null && (
              <ReferenceLine y={projection.safety_stock} stroke="#94a3b8" strokeDasharray="4 4"
                label={{ value: 'safety stock', fontSize: 11, fill: '#64748b' }} />
            )}
            <Line dataKey="do nothing" stroke="#94a3b8" strokeWidth={2} dot={false} />
            <Line dataKey="chosen" stroke="#0ea5e9" strokeWidth={2} />
            <Line dataKey="confirmed" stroke="#059669" strokeWidth={2} strokeDasharray="6 3" />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <p className="mt-1 text-xs text-slate-400">
        "chosen" is the engine's prediction for the decision; "confirmed" is re-projected after the supplier answered (outcome check).
      </p>
    </Card>
  )
}
