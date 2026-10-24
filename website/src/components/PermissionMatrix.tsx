// Every caller x every action, as the demo API answered. Each cell is a real
// request; pick one to see the exchange and the rule that decided it.
import { useState } from 'react';
import JsonView from './JsonView';

export interface MatrixCaller { id: string; role: string | null; workspace: string | null; color: string }
export interface MatrixAction { key: string; label: string; request: string }
export interface Cell {
  status: number;
  outcome: 'ok' | 'partial' | 'no';
  label: string;
  why: string;
  pin: number | null;
  pinColor: string | null;
  method: string;
  url: string;
  body: any;
  response: any;
}

const STATUS_TEXT: Record<number, string> = { 200: 'OK', 201: 'Created', 204: 'No Content', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found' };

export default function PermissionMatrix({ callers, actions, cells }: { callers: MatrixCaller[]; actions: MatrixAction[]; cells: Record<string, Record<string, Cell>> }) {
  const [sel, setSel] = useState<[string, string]>(['read_p2', 'victor']);
  const cell = cells[sel[0]][sel[1]];
  const caller = callers.find((c) => c.id === sel[1])!;
  const icon = { ok: 'icon-[lucide--check]', partial: 'icon-[lucide--circle-dashed]', no: 'icon-[lucide--x]' };
  return (
    <div className="space-y-4">
      <p className="md:hidden text-xs ink-3 flex items-center gap-1.5"><span className="icon-[lucide--move-horizontal] size-3.5" />Swipe the table to see every caller.</p>
      <div className="panel overflow-x-auto">
        <table className="matrix w-full min-w-[46rem] border-collapse text-sm">
          <thead>
            <tr>
              <th className="text-left font-normal ink-3 text-xs p-4 w-[30%]">Request</th>
              {callers.map((c) => (
                <th key={c.id} className="p-3 font-normal">
                  <span className="inline-flex flex-col items-center gap-1">
                    <span className="avatar" style={{ '--who': c.color } as React.CSSProperties}>{c.id === 'anonymous' ? '?' : c.id[0].toUpperCase()}</span>
                    <span className="font-semibold text-[.8rem]">{c.id}</span>
                    <span className="ink-3 text-[.66rem]">{c.role ? `${c.role} · ${c.workspace}` : 'not signed in'}</span>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {actions.map((a) => (
              <tr key={a.key} className="border-t border-[var(--line)]">
                <td className="p-4 align-middle">
                  <div className="font-semibold text-[.86rem]">{a.label}</div>
                  <div className="font-mono text-[.68rem] ink-3 mt-0.5">{a.request}</div>
                </td>
                {callers.map((c) => {
                  const x = cells[a.key][c.id];
                  const on = sel[0] === a.key && sel[1] === c.id;
                  return (
                    <td key={c.id} className="p-1.5 text-center align-middle">
                      <button
                        type="button"
                        onClick={() => setSel([a.key, c.id])}
                        aria-pressed={on}
                        className={`cell cell-${x.outcome} ${on ? 'is-on' : ''}`}
                        title={x.why}
                      >
                        <span className={`${icon[x.outcome]} size-3.5`} />
                        <span className="font-mono font-bold">{x.status}</span>
                        <span className="cell-label">{x.label}</span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="frame" key={sel.join('-')}>
        <div className="frame-bar"><i></i><i></i><i></i><span className="ml-2 truncate">{caller.id}{caller.role ? ` · ${caller.role} in ${caller.workspace}` : ' · not signed in'}</span></div>
        <div className="grid md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)] fade-swap">
          <div className="p-5 border-b md:border-b-0 md:border-r border-[var(--line)] font-mono text-[.76rem] leading-[1.6] min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={`method method-${cell.method.toLowerCase()}`}>{cell.method}</span>
              <span className="break-all">{cell.url}</span>
            </div>
            {cell.body && <JsonView value={cell.body} className="mt-3 ink-2" />}
            <p className="font-sans text-[.9rem] ink-2 leading-relaxed mt-5 flex gap-2.5 items-start">
              {cell.pin ? (
                <span className="pin pin-sm shrink-0" style={{ '--pin': cell.pinColor } as React.CSSProperties}>{cell.pin}</span>
              ) : (
                <span className={`${icon[cell.outcome]} size-4 shrink-0 mt-0.5 ${cell.outcome === 'no' ? 'text-[var(--color-error)]' : 'mint'}`} />
              )}
              <span>{cell.why}</span>
            </p>
          </div>
          <div className="p-5 font-mono text-[.74rem] leading-[1.55] min-w-0 max-h-[22rem] overflow-auto">
            <span className={`http ${cell.status < 300 ? 'http-2' : 'http-4'}`}>{cell.status} {STATUS_TEXT[cell.status] ?? ''}</span>
            {cell.response !== null ? <JsonView value={cell.response} highlight={['budget']} className="mt-3" /> : <div className="ink-3 mt-3">(empty body)</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
