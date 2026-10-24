// A JSON response, pretty-printed like the API returns it (two-space indent),
// with chosen keys lit up. Plain spans so it stays light.
import type { ReactNode } from 'react';

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };

interface Props {
  value: Json;
  highlight?: string[];
  className?: string;
}

export default function JsonView({ value, highlight = [], className = '' }: Props) {
  const lines: ReactNode[] = [];
  const lit = new Set(highlight);
  let n = 0;

  const scalar = (v: Json) => {
    if (v === null) return <span className="j-null">null</span>;
    if (typeof v === 'string') return <span className="j-str">"{v}"</span>;
    if (typeof v === 'number') return <span className="j-num">{v}</span>;
    if (typeof v === 'boolean') return <span className="j-num">{String(v)}</span>;
    return null;
  };

  const walk = (v: Json, indent: number, key: string | null, last: boolean) => {
    const pad = '  '.repeat(indent);
    const comma = last ? '' : ',';
    const label = key !== null ? <><span className="j-key">"{key}"</span><span className="j-p">: </span></> : null;
    const isLit = key !== null && lit.has(key);
    const row = (content: ReactNode) =>
      lines.push(
        <div key={n++} className={isLit ? 'j-lit' : undefined}>
          {pad}{content}
        </div>,
      );
    if (Array.isArray(v)) {
      if (!v.length) return row(<>{label}<span className="j-p">[]{comma}</span></>);
      row(<>{label}<span className="j-p">[</span></>);
      v.forEach((item, i) => walk(item, indent + 1, null, i === v.length - 1));
      lines.push(<div key={n++}>{pad}<span className="j-p">]{comma}</span></div>);
    } else if (v !== null && typeof v === 'object') {
      const entries = Object.entries(v);
      if (!entries.length) return row(<>{label}<span className="j-p">{'{}'}{comma}</span></>);
      row(<>{label}<span className="j-p">{'{'}</span></>);
      entries.forEach(([k, item], i) => walk(item, indent + 1, k, i === entries.length - 1));
      lines.push(<div key={n++}>{pad}<span className="j-p">{'}'}{comma}</span></div>);
    } else {
      row(<>{label}{scalar(v)}<span className="j-p">{comma}</span></>);
    }
  };

  walk(value, 0, null, true);
  return <pre className={`json-view ${className}`}>{lines}</pre>;
}
