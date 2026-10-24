// The hero infographic: the demo's real model on the left, with pins on the
// lines that decide who sees what; on the right, what GET /api/projects/
// actually returned to each caller, drawn as the rows of each workspace.
// Picking a caller lights the lines responsible for their result.
import { useState } from 'react';
import JsonView from './JsonView';

export interface Token { t: string; c: string; i?: boolean }
export interface Pin { id: number; color: string; lines: number[]; title: string; body: string }
export interface Row { id: number; title: string; workspace: string; owner: string; budget: string }
export interface Caller {
  id: string;
  role: string | null;
  workspace: string | null;
  color: string;
  status: number;
  total: number | null;
  response: any;
  visible: Record<string, string | null>;
  pins: number[];
  notes: { pin: number | null; text: string }[];
}

interface Props {
  code: Token[][];
  pins: Pin[];
  keys: { tenant: number; owner: number; sensitive: number };
  rows: Row[];
  callers: Caller[];
  roles: { role: string; perms: string[] }[];
}

export default function Anatomy({ code, pins, keys, rows, callers, roles }: Props) {
  const [active, setActive] = useState(2);
  const [hover, setHover] = useState<number | null>(null);
  const [raw, setRaw] = useState(false);
  const caller = callers[active];
  const lit = new Set(hover !== null ? [hover] : caller.pins);
  const pinByLine = new Map<number, Pin>();
  const firstLine = new Map<number, Pin>();
  for (const p of pins) {
    p.lines.forEach((l) => pinByLine.set(l, p));
    firstLine.set(p.lines[0], p);
  }
  const workspaces = [...new Set(rows.map((r) => r.workspace))];
  const pin = (id: number) => pins.find((p) => p.id === id)!;

  const Badge = ({ id, size = 'sm' }: { id: number; size?: 'sm' | 'xs' }) => (
    <span
      className={`pin pin-${size}`}
      style={{ '--pin': pin(id).color } as React.CSSProperties}
      onMouseEnter={() => setHover(id)}
      onMouseLeave={() => setHover(null)}
    >
      {id}
    </span>
  );

  return (
    <div className="frame anatomy grid lg:grid-cols-[minmax(0,1.08fr)_minmax(0,1fr)]">
      {/* The model */}
      <div className="min-w-0 order-2 lg:order-1 border-t lg:border-t-0 lg:border-r border-[var(--line)]">
        <div className="frame-bar"><i></i><i></i><i></i><span className="ml-2">projects/models.py</span></div>
        <pre className="code-lines py-4 text-[.78rem] sm:text-[.8rem] leading-[1.7] overflow-x-auto">
          {code.map((line, i) => {
            const p = pinByLine.get(i);
            const on = p && lit.has(p.id);
            const start = firstLine.get(i);
            return (
              <div
                key={i}
                className={`code-line ${on ? 'is-lit' : ''}`}
                style={p ? ({ '--pin': p.color } as React.CSSProperties) : undefined}
              >
                <span className="gutter">{start ? <Badge id={start.id} size="xs" /> : <span className="ln">{i + 1}</span>}</span>
                <span className="src">
                  {line.length ? line.map((tok, j) => (
                    <span key={j} style={{ color: tok.c, fontStyle: tok.i ? 'italic' : undefined }}>{tok.t}</span>
                  )) : ' '}
                </span>
              </div>
            );
          })}
        </pre>
        <div className="border-t border-[var(--line)] px-5 py-4">
          <div className="eyebrow !text-[.6rem] mb-2.5">Roles in settings.py</div>
          <div className="space-y-1.5">
            {roles.map((r) => (
              <div key={r.role} className={`flex flex-wrap items-center gap-1.5 text-[.72rem] ${caller.role === r.role ? '' : 'opacity-55'}`}>
                <span className="font-mono w-16 shrink-0 ink-2">{r.role}</span>
                {r.perms.map((perm) => (
                  <span key={perm} className={`perm ${perm.startsWith('budget') ? 'perm-budget' : ''}`}>{perm}</span>
                ))}
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* What each user gets */}
      <div className="min-w-0 order-1 lg:order-2 p-4 sm:p-5 flex flex-col gap-4">
        <div>
          <div className="eyebrow !text-[.6rem] mb-2.5">Choose a user</div>
          <div className="flex flex-wrap gap-2" role="tablist">
            {callers.map((c, i) => (
              <button
                key={c.id}
                type="button"
                role="tab"
                aria-selected={i === active}
                onClick={() => { setActive(i); setRaw(false); }}
                className={`caller ${i === active ? 'is-on' : ''}`}
                style={{ '--who': c.color } as React.CSSProperties}
              >
                <span className="avatar">{c.id === 'anonymous' ? '?' : c.id[0].toUpperCase()}</span>
                <span className="text-left leading-tight">
                  <span className="block font-semibold text-[.82rem]">{c.id}</span>
                  <span className="block ink-3 text-[.66rem]">{c.role ? `${c.role} · ${c.workspace}` : 'not signed in'}</span>
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="flex items-center gap-2 font-mono text-[.76rem] flex-wrap">
          <span className="method method-get">GET</span>
          <span>/api/projects/</span>
          <span className="ml-auto flex items-center gap-2">
            <span className={`http ${caller.status < 300 ? 'http-2' : 'http-4'}`}>{caller.status}</span>
            <span className="ink-3">{caller.total !== null ? `${caller.total} ${caller.total === 1 ? 'row' : 'rows'}` : 'no rows'}</span>
          </span>
        </div>

        {raw ? (
          <div className="rounded-xl bg-[var(--code-bg)] border border-[var(--line)] p-3 text-[.72rem] leading-[1.55] max-h-[22rem] overflow-auto fade-swap">
            <JsonView value={caller.response} highlight={['budget']} />
          </div>
        ) : (
          <div className="space-y-3 fade-swap" key={caller.id}>
            {workspaces.map((ws) => (
              <div key={ws} className={`ws ${caller.workspace === ws ? 'is-home' : ''}`}>
                <div className="ws-label">
                  <span className="icon-[lucide--building-2] size-3.5" />
                  {ws} workspace
                  {caller.workspace && caller.workspace !== ws && <Badge id={keys.tenant} />}
                </div>
                <ul className="space-y-1.5">
                  {rows.filter((r) => r.workspace === ws).map((r) => {
                    const seen = String(r.id) in caller.visible;
                    const budget = caller.visible[String(r.id)];
                    const why = !caller.role ? null : r.workspace !== caller.workspace ? keys.tenant : keys.owner;
                    return (
                      <li key={r.id} className={`row ${seen ? 'is-seen' : ''}`}>
                        <span className="owner" title={`owner: ${r.owner}`} style={{ '--who': callers.find((c) => c.id === r.owner)?.color } as React.CSSProperties}>
                          {r.owner[0].toUpperCase()}
                        </span>
                        <span className="truncate">{r.title}</span>
                        <span className="ml-auto flex items-center gap-1.5 shrink-0">
                          {seen ? (
                            budget !== null ? (
                              <span className="font-mono text-[.72rem] ink-2">${Number(budget).toLocaleString('en-US')}</span>
                            ) : (
                              <span className="hidden-chip"><span className="icon-[lucide--eye-off] size-3" />budget<Badge id={keys.sensitive} /></span>
                            )
                          ) : (
                            <span className="flex items-center gap-1 ink-3 text-[.7rem]">
                              <span className="icon-[lucide--lock] size-3" />not returned{why && caller.workspace === ws && <Badge id={why} />}
                            </span>
                          )}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}

        <ul className="space-y-2 text-[.84rem] fade-swap" key={`n-${caller.id}`}>
          {caller.notes.map((n) => (
            <li key={n.text} className="flex gap-2.5 items-start ink-2 leading-snug">
              {n.pin ? <Badge id={n.pin} /> : <span className="icon-[lucide--octagon-x] size-4 shrink-0 text-[var(--color-error)] mt-0.5" />}
              <span>{n.text}</span>
            </li>
          ))}
        </ul>

        <button type="button" className="self-start text-xs ink-3 hover:text-[var(--mint)] flex items-center gap-1.5 mt-auto" onClick={() => setRaw(!raw)}>
          <span className={raw ? 'icon-[lucide--layout-list] size-3.5' : 'icon-[lucide--braces] size-3.5'} />
          {raw ? 'Show as rows' : 'Show the JSON response'}
        </button>
      </div>
    </div>
  );
}
