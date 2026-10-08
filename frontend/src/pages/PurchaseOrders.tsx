import { useEffect, useState } from 'react'
import { api, type PurchaseOrder, type Transfer } from '../api'
import { Badge, Card, Json, fmt, statusTone } from '../ui'

export default function PurchaseOrders({ onOpenRun }: { onOpenRun: (id: number) => void }) {
  const [data, setData] = useState<{ scenario_id: string | null; purchase_orders: PurchaseOrder[]; transfers: Transfer[] } | null>(null)
  useEffect(() => { api.purchaseOrders().then(setData) }, [])
  if (!data) return <p className="text-slate-500">Loading…</p>

  return (
    <div className="space-y-4">
      <p className="text-sm text-slate-500">
        Workspace scenario: <code>{data.scenario_id ?? 'none loaded'}</code> (purchase orders belong to the currently loaded scenario).
      </p>
      <Card title={`Purchase orders (${data.purchase_orders.length})`}>
        <div className="space-y-3">
          {data.purchase_orders.map((po) => <PORow key={po.id} po={po} onOpenRun={onOpenRun} />)}
          {data.purchase_orders.length === 0 && <p className="text-sm text-slate-400">No purchase orders.</p>}
        </div>
      </Card>
      {data.transfers.length > 0 && (
        <Card title="Stock transfers">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th>Id</th><th>From → to</th><th>SKU</th><th>Qty</th><th>Arrives</th><th>Status</th></tr></thead>
            <tbody>
              {data.transfers.map((t) => (
                <tr key={t.id} className="border-t"><td className="py-1">{t.id}</td><td>{t.from} → {t.to}</td><td>{t.sku}</td><td>{t.qty}</td><td>{t.expected_arrival}</td><td><Badge tone={statusTone(t.status)}>{t.status}</Badge></td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  )
}

function PORow({ po, onOpenRun }: { po: PurchaseOrder; onOpenRun: (id: number) => void }) {
  const [open, setOpen] = useState(po.created_by === 'agent')
  return (
    <div className="rounded border border-slate-200">
      <button onClick={() => setOpen(!open)} className="flex w-full flex-wrap items-center gap-3 p-2 text-left text-sm hover:bg-slate-50">
        <span className="font-mono font-medium">{po.id}</span>
        <Badge tone={statusTone(po.status)}>{po.status}</Badge>
        <span>{po.supplier} → {po.node}</span>
        <span className="text-slate-500">
          {po.lines.map((l) => `${l.qty_ordered}${l.qty_confirmed != null && l.qty_confirmed !== l.qty_ordered ? ` (${l.qty_confirmed} confirmed)` : ''} × ${l.sku} @ ${fmt(l.unit_cost)} on ${l.expected_arrival}`).join(' + ')}
        </span>
        <span className="ml-auto text-xs text-slate-400">by {po.created_by}</span>
      </button>
      {open && (
        <div className="border-t border-slate-100 p-2">
          {po.run_id && <button onClick={() => onOpenRun(po.run_id!)} className="mb-2 text-xs text-sky-700 hover:underline">open run #{po.run_id}</button>}
          <ol className="space-y-1 border-l-2 border-slate-200 pl-3 text-sm">
            {po.events.map((e, i) => (
              <li key={i}>
                <span className="text-xs text-slate-400">{e.at.replace('T', ' ')}</span>{' '}
                <Badge tone={statusTone(e.type)}>{e.type}</Badge>{' '}
                <span className="text-xs text-slate-500">by {e.source}</span>
                {Object.keys(e.payload ?? {}).length > 0 && <details className="ml-2 inline text-xs"><summary className="inline cursor-pointer text-slate-400">details</summary><Json value={e.payload} /></details>}
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}
