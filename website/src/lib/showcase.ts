// Builds the home page's infographics from the demo's real source and the
// responses tools/demo/capture.py recorded (data/demo.json): the pinned model,
// each caller's view of GET /api/projects/, and the permission matrix.
import { codeToTokens } from 'shiki';
import theme from '../styles/shiki-turbo.json';
import demo from '../data/demo.json';
import modelsSrc from '../../tools/demo/demo/models.py?raw';
import settingsSrc from '../../tools/demo/settings.py?raw';
import type { Caller, Pin, Token } from '../components/Anatomy';
import type { Cell, MatrixAction, MatrixCaller } from '../components/PermissionMatrix';

const COLORS = { alice: '#3ee6a0', maya: '#38d6f0', victor: '#f4c97a', bob: '#b69bff', anonymous: '#8ea39a' } as Record<string, string>;
const m = demo.matrix;
const rows = m.rows;
const people = demo.users as Record<string, { role: string; workspace: string }>;

export const MODEL_CODE = 'from turbodrf.mixins import TurboDRFMixin\n\n' + modelsSrc.slice(modelsSrc.indexOf('class Project')).trimEnd();

/** Pins, numbered in the order they appear in the code. */
function buildPins(lines: string[]): (Pin & { key: string })[] {
  const at = (needle: string) => lines.findIndex((l) => l.includes(needle));
  const fieldsStart = at('"fields": [');
  const fieldsEnd = lines.findIndex((l, i) => i > fieldsStart && l.trim() === '],');
  const spec = [
    { key: 'search', lines: [at('searchable_fields')], color: '#b69bff', title: 'Search', body: '?search= matches against the title field.' },
    { key: 'sensitive', lines: [at('sensitive_fields')], color: '#fb7185', title: 'Sensitive field', body: 'budget is returned only to roles with demo.project.budget.read, and only roles with .write can change it.' },
    { key: 'fields', lines: Array.from({ length: fieldsEnd - fieldsStart + 1 }, (_, i) => fieldsStart + i), color: '#38d6f0', title: 'Fields', body: 'The fields the API returns. client__name follows the client foreign key.' },
    { key: 'tenant', lines: [at('"tenant_field"')], color: '#3ee6a0', title: 'Tenancy', body: "Every query is filtered to the user's workspace." },
    { key: 'owner', lines: [at('"owner_field"'), at('"bypass_owner_roles"')], color: '#f4c97a', title: 'Owner rule', body: 'Within a workspace, users see only the projects they own. admin and manager see all of them.' },
  ];
  return spec.map((p, i) => ({ ...p, id: i + 1 }));
}

const codeLines = MODEL_CODE.split('\n');
export const PINS = buildPins(codeLines);
export const PIN = Object.fromEntries(PINS.map((p) => [p.key, p])) as Record<string, Pin & { key: string }>;

export async function modelTokens(): Promise<Token[][]> {
  const { tokens } = await codeToTokens(MODEL_CODE, { lang: 'python', theme: theme as any });
  return tokens.map((line) => line.map((t) => ({ t: t.content, c: t.color ?? '#d7e3dd', i: ((t.fontStyle ?? 0) & 1) === 1 })));
}

/** The roles from the demo's settings.py, without the app/model prefix. */
export function roleTable() {
  const roles: { role: string; perms: string[] }[] = [];
  const re = /"(\w+)": \[([^\]]*)\]/g;
  const block = settingsSrc.slice(settingsSrc.indexOf('TURBODRF_ROLES'));
  for (const match of block.matchAll(re)) {
    const perms = [...match[2].matchAll(/"demo\.project\.([\w.]+)"/g)].map((x) => x[1]);
    roles.push({ role: match[1], perms });
  }
  return roles;
}

// ---------------------------------------------------------------- callers

const NOTES: Record<string, { pin: string | null; text: string }[]> = {
  alice: [
    { pin: 'tenant', text: "Only projects in Acme, alice's workspace." },
    { pin: 'owner', text: 'admin is exempt from the owner rule, so alice gets every Acme project.' },
    { pin: 'sensitive', text: 'budget is included because admin has demo.project.budget.read.' },
  ],
  maya: [
    { pin: 'tenant', text: "Only projects in Acme, maya's workspace." },
    { pin: 'owner', text: 'manager is exempt from the owner rule, so maya gets every Acme project.' },
    { pin: 'sensitive', text: 'budget is included because manager has demo.project.budget.read.' },
  ],
  victor: [
    { pin: 'tenant', text: "Only projects in Acme, victor's workspace." },
    { pin: 'owner', text: "viewer isn't exempt from the owner rule, so victor only gets the project he owns." },
    { pin: 'sensitive', text: "budget is left out because viewer doesn't have demo.project.budget.read." },
  ],
  bob: [{ pin: 'tenant', text: "Only projects in Globex, bob's workspace. Acme's projects return 404 for him, even though he's an admin." }],
  anonymous: [{ pin: null, text: "Not signed in, so the request is refused with a 403. Projects aren't marked public_access." }],
};

export function anatomyCallers(): Caller[] {
  return m.callers.map((id) => {
    const cell = (m.actions.list as Record<string, any>)[id];
    const data = cell.response?.data as any[] | undefined;
    const visible: Record<string, string | null> = {};
    data?.forEach((r) => (visible[String(r.id)] = 'budget' in r ? r.budget : null));
    const notes = NOTES[id].map((n) => ({ pin: n.pin ? PIN[n.pin].id : null, text: n.text }));
    return {
      id,
      role: people[id]?.role ?? null,
      workspace: people[id]?.workspace ?? null,
      color: COLORS[id],
      status: cell.status,
      total: cell.response?.pagination?.total_items ?? null,
      response: cell.response,
      visible,
      pins: notes.filter((n) => n.pin).map((n) => n.pin as number),
      notes,
    };
  });
}

// ----------------------------------------------------------------- matrix

const row = (id: number) => rows.find((r) => r.id === id)!;
const urlId = (url: string) => Number(url.match(/\/(\d+)\/$/)?.[1]);
const title = (id: number) => row(id).title;

export const MATRIX_ACTIONS: MatrixAction[] = [
  { key: 'list', label: 'List the projects', request: 'GET /api/projects/', highlight: ['budget'] },
  { key: 'read_p1', label: `Read "${title(1)}"`, request: `GET /api/projects/1/ · owner ${row(1).owner}`, highlight: ['budget'] },
  { key: 'read_p2', label: `Read "${title(2)}"`, request: `GET /api/projects/2/ · owner ${row(2).owner}`, highlight: ['budget'] },
  { key: 'edit_status', label: `Change the status of "${title(1)}"`, request: 'PATCH /api/projects/1/ {status}', highlight: ['status'] },
  { key: 'edit_budget', label: `Change the budget of "${title(1)}"`, request: 'PATCH /api/projects/1/ {budget}', highlight: ['budget'] },
  { key: 'create_foreign', label: "Create a project with another workspace's client", request: 'POST /api/projects/ {client}', highlight: ['client'] },
  { key: 'delete', label: `Delete "${title(m.spare_pk)}"`, request: `DELETE /api/projects/${m.spare_pk}/`, highlight: [] },
];

const PERM: Record<string, string> = {
  list: 'read', read_p1: 'read', read_p2: 'read', edit_status: 'update', edit_budget: 'update', create_foreign: 'create', delete: 'delete',
};

function describe(action: string, who: string, c: any): Omit<Cell, 'method' | 'url' | 'body' | 'response' | 'status' | 'pinColor'> {
  const role = people[who]?.role;
  const ws = people[who]?.workspace;
  const r = c.response;
  if (c.status === 403 && who === 'anonymous') return { outcome: 'no', label: 'not signed in', why: "Not signed in, and projects aren't marked public_access.", pin: null };
  if (c.status === 403) return { outcome: 'no', label: 'not allowed', why: `${role} has no demo.project.${PERM[action]}.`, pin: null };
  if (c.status === 404) {
    const target = row(urlId(c.url));
    return target.workspace !== ws
      ? { outcome: 'no', label: 'not visible', why: `"${target.title}" is in ${target.workspace}. To a ${ws} user that project doesn't exist, so the answer is 404, not 403.`, pin: PIN.tenant.id }
      : { outcome: 'no', label: 'not visible', why: `"${target.title}" belongs to ${target.owner}, and ${role} isn't exempt from the owner rule.`, pin: PIN.owner.id };
  }
  if (c.status === 400) return { outcome: 'no', label: 'rejected', why: "That client belongs to the other workspace. A write can only point at rows the user can see, and a hidden row gets the same 400 as a missing one.", pin: PIN.tenant.id };
  if (action === 'list') {
    const n = r.pagination.total_items;
    const hidden = !r.data.some((x: any) => 'budget' in x);
    return hidden
      ? { outcome: 'partial', label: `${n} row, no budget`, why: `Only the project ${who} owns, without budget: ${role} doesn't have budget.read.`, pin: PIN.owner.id }
      : { outcome: 'ok', label: `${n} ${n === 1 ? 'row' : 'rows'}`, why: `Every project in ${ws} that ${who} is allowed to see, including budget.`, pin: PIN.tenant.id };
  }
  if (action.startsWith('read')) {
    return 'budget' in r
      ? { outcome: 'ok', label: 'all fields', why: `${who} can see this project, including budget.`, pin: null }
      : { outcome: 'partial', label: 'no budget', why: `${who} can read the project, but budget is a sensitive field and ${role} doesn't have budget.read.`, pin: PIN.sensitive.id };
  }
  if (action === 'edit_budget') {
    return r.budget === c.body.budget
      ? { outcome: 'ok', label: 'changed', why: `${role} has demo.project.budget.write, so budget is now ${r.budget}.`, pin: PIN.sensitive.id }
      : { outcome: 'partial', label: 'budget ignored', why: `${role} can update projects but doesn't have budget.write, so TurboDRF ignores the new budget and it stays ${r.budget}.`, pin: PIN.sensitive.id };
  }
  if (action === 'delete') return { outcome: 'ok', label: 'deleted', why: `${role} has demo.project.delete.`, pin: null };
  return { outcome: 'ok', label: 'saved', why: `${role} has demo.project.update.`, pin: null };
}

export function matrixCallers(): MatrixCaller[] {
  return m.callers.map((id) => ({ id, role: people[id]?.role ?? null, workspace: people[id]?.workspace ?? null, color: COLORS[id] }));
}

export function matrixCells(): Record<string, Record<string, Cell>> {
  const out: Record<string, Record<string, Cell>> = {};
  for (const a of MATRIX_ACTIONS) {
    out[a.key] = {};
    for (const who of m.callers) {
      const c = (m.actions as Record<string, Record<string, any>>)[a.key][who];
      const d = describe(a.key, who, c);
      out[a.key][who] = {
        ...d,
        pinColor: d.pin ? PINS[d.pin - 1].color : null,
        status: c.status,
        method: c.method,
        url: c.url,
        body: c.body,
        response: c.response,
      };
    }
  }
  return out;
}
