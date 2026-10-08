import { useEffect, useState } from 'react'
import { api, type EvalResults, type EvalRun } from '../api'
import { Badge, Card } from '../ui'

const DIMS = ['decision', 'information', 'constraints', 'action', 'validation', 'recovery']
const LABEL: Record<string, string> = { scripted: 'scripted', gemini: 'Gemini', openai_compat: 'Groq' }

function rate(values: (boolean | null | undefined)[]) {
  const vals = values.filter((v): v is boolean => v != null)
  if (vals.length === 0) return 'n/a'
  const ok = vals.filter(Boolean).length
  return `${Math.round((ok / vals.length) * 100)}% (${ok}/${vals.length})`
}

function cell(runs: EvalRun[], pick: (r: EvalRun) => boolean | null | undefined) {
  const vals = runs.map(pick).filter((v): v is boolean => v != null)
  if (vals.length === 0) return <span className="text-slate-300">–</span>
  const ok = vals.filter(Boolean).length
  return ok === vals.length ? <span className="text-emerald-600">✓</span>
    : ok === 0 ? <span className="text-rose-600">✗</span>
      : <span className="text-amber-600">{ok}/{vals.length}</span>
}

export default function Evals() {
  const [results, setResults] = useState<EvalResults[]>([])
  useEffect(() => { api.evals().then(setResults) }, [])
  const cases = [...new Set(results.flatMap((r) => r.runs.map((x) => x.case)))].sort()
  const graded = (runs: EvalRun[]) => runs.filter((x) => !x.infra_error) // infrastructure failures are not judged
  const failures = results.flatMap((r) => graded(r.runs).filter((x) => !x.passed).map((x) => ({ ...x, provider: r.provider })))
  const incomplete = results.flatMap((r) => r.runs.filter((x) => x.infra_error).map((x) => ({ ...x, provider: r.provider })))

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-500">
        Scripted runs replay recorded trajectories through the real system and must pass 100%: they test the system.
        Gemini and Groq runs let the model make every choice: they measure its judgement. Run <code>make eval</code> or
        <code> make eval-real</code> to refresh; the report is <code>evals/report.md</code>.
      </p>
      <Card title="Pass rate by provider">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr><th className="py-1">Provider</th><th>Model</th><th>Runs</th><th>Overall</th>{DIMS.map((d) => <th key={d}>{d}</th>)}<th>model resisted</th><th>system safe</th><th>did not complete</th><th>updated</th></tr>
            </thead>
            <tbody>
              {results.map((r) => (
                <tr key={r.provider} className="border-t">
                  <td className="py-1.5 font-medium">{LABEL[r.provider] ?? r.provider}</td>
                  <td className="font-mono text-xs">{r.model}</td>
                  <td>{r.runs.length}</td>
                  <td className="font-semibold">{rate(graded(r.runs).map((x) => x.passed))}</td>
                  {DIMS.map((d) => <td key={d}>{rate(graded(r.runs).map((x) => x.dimensions[d]))}</td>)}
                  <td>{rate(graded(r.runs).map((x) => x.extras?.model_resisted))}</td>
                  <td>{rate(graded(r.runs).map((x) => x.extras?.system_safe))}</td>
                  <td>{r.runs.filter((x) => x.infra_error).length || '–'}</td>
                  <td className="text-xs text-slate-400">{r.updated_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {results.length === 0 && <p className="text-sm text-slate-400">No results yet: run <code>make eval</code>.</p>}
      </Card>

      <Card title="Results by case" right={<span className="text-xs text-slate-400">✓ all runs passed · ✗ none · k/n some · – not applicable</span>}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr><th className="py-1">Case</th><th>Ran on</th><th>Passed</th>{DIMS.map((d) => <th key={d}>{d}</th>)}<th>Last outcome</th><th>Avg time</th><th>Judge</th></tr>
            </thead>
            <tbody>
              {cases.flatMap((c) => results.filter((r) => r.runs.some((x) => x.case === c)).map((r) => {
                const runs = r.runs.filter((x) => x.case === c)
                const last = runs[runs.length - 1]
                const judged = runs.map((x) => x.judge?.average).filter((v): v is number => v != null)
                return (
                  <tr key={`${c}-${r.provider}`} className="border-t">
                    <td className="py-1 font-mono text-xs">{c}</td>
                    <td>{LABEL[r.provider] ?? r.provider}</td>
                    <td>{runs.filter((x) => x.passed).length}/{runs.length}</td>
                    {DIMS.map((d) => <td key={d}>{cell(graded(runs), (x) => x.dimensions[d])}</td>)}
                    <td className="text-xs">{last.error ? <Badge tone="red">error</Badge> : `${last.outcome ?? '–'} ${last.quantity ?? ''}`}</td>
                    <td className="text-xs">{(runs.reduce((s, x) => s + x.duration_s, 0) / runs.length).toFixed(1)}s</td>
                    <td className="text-xs">{judged.length ? (judged.reduce((a, b) => a + b, 0) / judged.length).toFixed(1) : '–'}</td>
                  </tr>
                )
              }))}
            </tbody>
          </table>
        </div>
      </Card>

      {incomplete.length > 0 && (
        <Card title={`Did not complete: infrastructure (${incomplete.length}, excluded from pass rates)`}>
          <ul className="space-y-1 text-sm">
            {incomplete.map((f) => (
              <li key={`${f.provider}-${f.case}-${f.run}`}>
                <span className="font-mono text-xs">{f.case}</span> on {LABEL[f.provider]}: <Badge tone="amber">{f.infra_error}</Badge>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title={`Failures (${failures.length})`}>
        <ul className="space-y-2 text-sm">
          {failures.map((f) => (
            <li key={`${f.provider}-${f.case}-${f.run}`}>
              <span className="font-mono text-xs">{f.case}</span> on {LABEL[f.provider]} (run {f.run}):{' '}
              <span className="text-slate-600">{f.error ?? Object.entries(f.failures).map(([k, v]) => `${k}: ${v}`).join('; ')}</span>
            </li>
          ))}
          {failures.length === 0 && <li className="text-slate-400">None.</li>}
        </ul>
      </Card>
    </div>
  )
}
