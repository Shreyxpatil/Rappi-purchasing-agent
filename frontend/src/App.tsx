import { useEffect, useState } from 'react'
import { api, type RunSummary } from './api'
import Launcher from './pages/Launcher'
import Approvals from './pages/Approvals'
import Evals from './pages/Evals'
import PurchaseOrders from './pages/PurchaseOrders'
import RunView from './pages/RunView'
import { Card, RunStatus } from './ui'

type Page = 'launch' | 'run' | 'approvals' | 'pos' | 'evals'

const NAV: { id: Page; label: string }[] = [
  { id: 'launch', label: 'Scenarios' },
  { id: 'run', label: 'Runs' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'pos', label: 'Purchase orders' },
  { id: 'evals', label: 'Evaluations' },
]

export default function App() {
  const [page, setPage] = useState<Page>('launch')
  const [runId, setRunId] = useState<number | null>(null)
  const openRun = (id: number) => {
    setRunId(id)
    setPage('run')
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3">
          <span className="font-semibold text-slate-900">AI Purchasing Agent</span>
          <nav className="flex gap-1">
            {NAV.map((n) => (
              <button
                key={n.id}
                onClick={() => setPage(n.id)}
                className={`rounded px-3 py-1.5 text-sm ${page === n.id ? 'bg-slate-900 text-white' : 'text-slate-600 hover:bg-slate-100'}`}
              >
                {n.label}
              </button>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        {page === 'launch' && <Launcher onOpenRun={openRun} />}
        {page === 'run' && (runId ? <RunView runId={runId} onApprovals={() => setPage('approvals')} /> : <RunList onOpenRun={openRun} />)}
        {page === 'approvals' && <Approvals onOpenRun={openRun} />}
        {page === 'pos' && <PurchaseOrders onOpenRun={openRun} />}
        {page === 'evals' && <Evals />}
      </main>
    </div>
  )
}

function RunList({ onOpenRun }: { onOpenRun: (id: number) => void }) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  useEffect(() => { api.runs().then(setRuns) }, [])
  return (
    <Card title="Runs">
      <table className="w-full text-sm">
        <thead className="text-left text-slate-500"><tr><th>#</th><th>Scenario</th><th>Provider</th><th>Status</th><th>Decision</th></tr></thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.id} onClick={() => onOpenRun(r.id)} className="cursor-pointer border-t hover:bg-slate-50">
              <td className="py-1.5">{r.id}</td><td className="font-mono text-xs">{r.scenario_id}</td><td>{r.provider}</td>
              <td><RunStatus status={r.status} reason={r.reason} /></td><td>{r.outcome ?? '–'} {r.quantity ?? ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}
