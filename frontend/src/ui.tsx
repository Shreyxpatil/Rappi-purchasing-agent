// Small shared presentational pieces.
import type { ReactNode } from 'react'

const TONES: Record<string, string> = {
  green: 'bg-emerald-100 text-emerald-800',
  red: 'bg-rose-100 text-rose-800',
  amber: 'bg-amber-100 text-amber-800',
  blue: 'bg-sky-100 text-sky-800',
  gray: 'bg-slate-100 text-slate-700',
}

export function Badge({ children, tone = 'gray' }: { children: ReactNode; tone?: keyof typeof TONES }) {
  return <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>{children}</span>
}

export function statusTone(status: string): keyof typeof TONES {
  if (['COMPLETED', 'CONFIRMED', 'AUTO', 'APPROVED', 'PLANNED', 'pass'].includes(status)) return 'green'
  if (['FAILED', 'REJECTED', 'BLOCK', 'CANCELLED', 'fail', 'SUPERSEDED'].includes(status)) return 'red'
  if (['AWAITING_APPROVAL', 'APPROVAL', 'PENDING', 'PARTIALLY_CONFIRMED', 'ESCALATED', 'warning', 'overridden'].includes(status)) return 'amber'
  if (['RUNNING', 'SUBMITTED'].includes(status)) return 'blue'
  return 'gray'
}

/** Status badge that names the reason when a run failed or escalated: "FAILED · LLM_QUOTA_EXHAUSTED". */
export function RunStatus({ status, reason }: { status: string; reason?: string | null }) {
  return <Badge tone={statusTone(status)}>{reason ? `${status} · ${reason}` : status}</Badge>
}

export function Card({ title, children, right }: { title?: ReactNode; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      {(title || right) && (
        <div className="mb-3 flex items-center justify-between gap-2">
          <h2 className="font-semibold text-slate-900">{title}</h2>
          {right}
        </div>
      )}
      {children}
    </section>
  )
}

export function Json({ value }: { value: unknown }) {
  return (
    <pre className="max-h-72 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

export const fmt = (n: number | null | undefined) => (n == null ? '–' : n.toLocaleString('en-US', { maximumFractionDigits: 2 }))
