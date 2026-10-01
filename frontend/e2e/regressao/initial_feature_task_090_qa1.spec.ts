import { Page, Request, Route, expect, test } from '@playwright/test';

type Classification = 'required' | 'nice_to_have';
type Level = 'junior' | 'mid-level' | 'senior' | 'expert';

interface Item {
  id: string | null;
  name: string;
  original_terms: string[];
  classification: Classification;
  level: Level | null;
  pending_clarification?: boolean;
  clarification_question?: string | null;
}

interface Message {
  id: string;
  role: 'candidate' | 'assistant';
  kind: string;
  content: string;
  created_at: string;
}

const CREATED_AT = '2026-10-01T15:08:02.203591Z';
const CLARIFICATION_QUESTION = 'Which cloud provider is meant?';

const ME = {
  id: 'u-1',
  email: 'alice@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
};

function item(id: string, name: string, classification: Classification, level: Level | null): Item {
  return {
    id,
    name,
    original_terms: [name],
    classification,
    level,
    pending_clarification: false,
    clarification_question: null,
  };
}

const REQUIREMENT_MESSAGES: Message[] = [
  {
    id: 'm-1',
    role: 'assistant',
    kind: 'info',
    content: 'Please paste the job requirements for the position you are preparing for.',
    created_at: CREATED_AT,
  },
  {
    id: 'm-2',
    role: 'candidate',
    kind: 'requirements',
    content: 'Required: Python at senior level, PostgreSQL, Docker/Kubernetes nice to have.',
    created_at: CREATED_AT,
  },
  {
    id: 'm-3',
    role: 'assistant',
    kind: 'requirements_reply',
    content:
      'Here is the structured list of the job requirements. Review it and confirm it to continue.',
    created_at: CREATED_AT,
  },
  {
    id: 'm-4',
    role: 'assistant',
    kind: 'clarification_request',
    content: CLARIFICATION_QUESTION,
    created_at: CREATED_AT,
  },
];

function initialItems(): Item[] {
  return [
    item('i-python', 'Python', 'required', 'senior'),
    item('i-postgresql', 'PostgreSQL', 'required', null),
    item('i-postgres', 'Postgres', 'nice_to_have', null),
    item('i-docker', 'Docker', 'nice_to_have', null),
    item('i-kubernetes', 'Kubernetes', 'nice_to_have', null),
  ];
}

/** `SessionView` (plan 8.1, CT-40) in `awaiting_confirmation` with the given list. */
function awaitingView(id: string, items: Item[]): Record<string, unknown> {
  return {
    id,
    status: 'awaiting_confirmation',
    created_at: CREATED_AT,
    language: 'en',
    interview_level: null,
    resume_name: 'good.pdf',
    messages: REQUIREMENT_MESSAGES,
    requirements: {
      items: items.map((entry, index) => ({
        ...entry,
        id: entry.id ?? `new-${index}`,
        pending_clarification: entry.pending_clarification ?? false,
        clarification_question: entry.clarification_question ?? null,
      })),
      non_technical: ['Fluent English'],
    },
    proposal: null,
    counter: null,
    current_question: null,
    answered: [],
    report_available: false,
  };
}

function inInterviewView(id: string): Record<string, unknown> {
  return {
    ...awaitingView(id, initialItems()),
    status: 'in_interview',
    proposal: { planned_count: 3, skills: ['Python', 'PostgreSQL'] },
    counter: { planned: 3, answered: 0, remaining: 3 },
    current_question: {
      id: 'q-1',
      position: 1,
      skill: 'Python',
      text: 'How does the GIL affect CPU-bound threads?',
    },
  };
}

async function mockBackend(page: Page, sessionId: string, view: Record<string, unknown>) {
  await page.route('**/api/**', (route: Route) =>
    route.fulfill({
      status: 404,
      json: { error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found.' } },
    }),
  );
  await page.route('**/api/auth/me', (route: Route) => route.fulfill({ json: ME }));
  await page.route(`**/api/sessions/${sessionId}`, (route: Route) => route.fulfill({ json: view }));
}

function bodyItems(request: Request): Item[] {
  return (request.postDataJSON() as { items: Item[] }).items;
}

function find(items: Item[], name: string): Item | undefined {
  return items.find((entry) => entry.name === name);
}

test('PLAN-05 — Edições rápidas no editor de requisitos se perdem / UI diverge do servidor', async ({
  page,
}) => {
  const sessionId = 'qa1-fast-edits';
  await mockBackend(page, sessionId, awaitingView(sessionId, initialItems()));

  // Each PUT answers with the list it received; the first answer arrives late, so with
  // concurrent requests it would be delivered after the second one.
  const puts: Item[][] = [];
  await page.route(`**/api/sessions/${sessionId}/requirement-list`, async (route: Route) => {
    const items = bodyItems(route.request());
    puts.push(items);
    if (puts.length === 1) {
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    await route.fulfill({ json: awaitingView(sessionId, items) });
  });

  await page.goto(`/sessions/${sessionId}`);
  const docker = page.getByRole('combobox', { name: 'Classification of Docker' });
  const pythonLevel = page.getByRole('combobox', { name: 'Expected level of Python' });
  const kubernetes = page.getByRole('combobox', { name: 'Classification of Kubernetes' });
  await expect(docker).toHaveValue('nice_to_have');

  const firstPut = page.waitForRequest(
    (request) => request.method() === 'PUT' && request.url().endsWith('/requirement-list'),
  );
  await docker.selectOption('Required');
  await pythonLevel.selectOption('Expert');
  await kubernetes.selectOption('Required');

  expect(find(bodyItems(await firstPut), 'Docker')?.classification).toBe('required');

  // The screen ends with every edit applied.
  await expect(docker).toHaveValue('required');
  await expect(pythonLevel).toHaveValue('expert');
  await expect(kubernetes).toHaveValue('required');
  await expect(page.getByRole('button', { name: 'Confirm list' })).toBeEnabled();

  // The last list stored by the server holds every edit, so it matches the screen.
  const stored = puts[puts.length - 1];
  expect(find(stored, 'Docker')?.classification).toBe('required');
  expect(find(stored, 'Python')?.level).toBe('expert');
  expect(find(stored, 'Kubernetes')?.classification).toBe('required');
  // Every request is built on the list the server returned before it: no edit is lost.
  for (const [index, list] of puts.entries()) {
    if (index > 0) {
      expect(find(list, 'Docker')?.classification).toBe('required');
    }
  }
});

test('PLAN-04 — Pergunta de esclarecimento dos requisitos some do chat e aparece como "Clarifications" da entrevista', async ({
  page,
}) => {
  const awaitingId = 'qa1-requirements-clarification';
  await mockBackend(page, awaitingId, awaitingView(awaitingId, initialItems()));
  await page.goto(`/sessions/${awaitingId}`);

  await expect(page.getByRole('log', { name: 'Conversation' })).toContainText(
    CLARIFICATION_QUESTION,
  );

  const interviewId = 'qa1-interview';
  await page.route(`**/api/sessions/${interviewId}`, (route: Route) =>
    route.fulfill({ json: inInterviewView(interviewId) }),
  );
  await page.goto(`/sessions/${interviewId}`);

  await expect(
    page.getByRole('heading', { name: 'How does the GIL affect CPU-bound threads?' }),
  ).toBeVisible();
  await expect(page.getByRole('log', { name: 'Clarifications' })).toHaveCount(0);
  await expect(page.getByText(CLARIFICATION_QUESTION)).toHaveCount(0);
});
