import { useEffect, useState } from 'react'
import { api, type RunDetail, type Step } from '../api'
import { Badge, Card, Json, statusTone } from '../ui'

const KIND_LABEL: Record<string, string> = {
  llm: 'model', tool: 'tool', decision: 'decision', policy: 'policy gate', validation: 'post-action diff',
  supplier: 'supplier', verification: 'outcome check', approval: 'approval', escalation: 'escalation',
  narrative_check: 'narrative check', nudge: 'nudge', intake: 'intake', error: 'error',
}

export default function RunView({ runId }: { runId: number }) {
  const [run, setRun] = useState<RunDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const r = await api.run(runId)
        if (!alive) return
        setRun(r)
        if (r.status === 'RUNNING') timer = setTimeout(load, 1500) // poll while the agent works
      } catch (e) {
        setError(String(e))
      }
    }
    load()
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [runId])

  if (error) return <p className="text-rose-700">{error}</p>
  if (!run) return <p className="text-slate-500">Loading run #{runId}…</p>

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Run #{run.id} · <code>{run.scenario_id}</code></h1>
        <Badge tone={statusTone(run.status)}>{run.status}</Badge>
        <span className="text-sm text-slate-500">provider {run.provider} · state {run.state} · replans {run.replans}</span>
        {run.status === 'RUNNING' && <span className="animate-pulse text-sm text-sky-700">running…</span>}
      </div>
      {run.error && <p className="rounded bg-rose-50 p-2 text-sm text-rose-700">{run.error}</p>}
      <Timeline steps={run.steps} />
    </div>
  )
}

function Timeline({ steps }: { steps: Step[] }) {
  // Transitions split the trace into the state-machine phases.
  const sections: { state: string; steps: Step[] }[] = []
  for (const s of steps) {
    if (s.kind === 'transition') {
      sections.push({ state: s.name.split('->')[1], steps: [] })
    } else {
      if (sections.length === 0) sections.push({ state: s.state, steps: [] })
      sections[sections.length - 1].steps.push(s)
    }
  }
  return (
    <Card title="Timeline" right={<span className="text-xs text-slate-400">{steps.length} steps · click a step for input/output</span>}>
      <ol className="space-y-3">
        {sections.map((sec, i) => (
          <li key={i}>
            <div className="mb-1 text-xs font-semibold tracking-wide text-slate-500 uppercase">{sec.state}</div>
            <ul className="space-y-1 border-l-2 border-slate-200 pl-3">
              {sec.steps.map((s) => <StepRow key={s.seq} step={s} />)}
              {sec.steps.length === 0 && <li className="text-xs text-slate-400">—</li>}
            </ul>
          </li>
        ))}
      </ol>
    </Card>
  )
}

function StepRow({ step }: { step: Step }) {
  const [open, setOpen] = useState(false)
  const err = (step.output as { error?: { code?: string } })?.error?.code
  return (
    <li>
      <button onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-sm hover:bg-slate-50">
        <span className={`h-2 w-2 shrink-0 rounded-full ${step.ok ? 'bg-emerald-500' : 'bg-rose-500'}`} />
        <Badge tone={step.kind === 'llm' ? 'blue' : 'gray'}>{KIND_LABEL[step.kind] ?? step.kind}</Badge>
        <span className="font-mono text-xs">{step.name}</span>
        {err && <Badge tone="red">{err}</Badge>}
        <span className="ml-auto text-xs text-slate-400">
          {step.latency_ms} ms{step.tokens_in != null ? ` · ${step.tokens_in}/${step.tokens_out} tok` : ''}
        </span>
      </button>
      {open && (
        <div className="mt-1 grid gap-2 md:grid-cols-2">
          <div><div className="text-xs text-slate-500">input</div><Json value={step.input} /></div>
          <div><div className="text-xs text-slate-500">output</div><Json value={step.output} /></div>
        </div>
      )}
    </li>
  )
}
