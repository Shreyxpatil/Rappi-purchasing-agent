import { useState } from 'react'

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
        <p className="text-sm text-slate-500">Page: {page}</p>
      </main>
    </div>
  )
}
