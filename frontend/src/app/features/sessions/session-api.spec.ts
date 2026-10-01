import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Observable, firstValueFrom } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import {
  InterviewOptions,
  RequirementItem,
  SessionApi,
  SessionMessage,
  SessionSummary,
  SessionView,
} from './session-api';

const SESSION_ID = 's-1';
const BASE = `/api/sessions/${SESSION_ID}`;

const VIEW: SessionView = {
  id: SESSION_ID,
  status: 'in_interview',
  created_at: '2026-10-01T10:00:00Z',
  language: 'en',
  interview_level: 'senior',
  resume_name: 'resume.pdf',
  messages: [],
  requirements: null,
  proposal: { planned_count: 2, skills: ['Python', 'SQL'] },
  counter: { planned: 2, answered: 0, remaining: 2 },
  current_question: { id: 'q-1', position: 1, skill: 'Python', text: 'What is a generator?' },
  answered: [],
  report_available: false,
};

const ITEM: RequirementItem = {
  id: 'r-1',
  name: 'Python',
  original_terms: ['python'],
  classification: 'required',
  level: 'senior',
  pending_clarification: false,
  clarification_question: null,
};

describe('SessionApi', () => {
  let api: SessionApi;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(SessionApi);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  async function expectViewCall(
    call: Observable<SessionView>,
    method: string,
    url: string,
    body: unknown,
  ): Promise<void> {
    const result = firstValueFrom(call);
    const req = httpTesting.expectOne(url);
    expect(req.request.method).toBe(method);
    expect(req.request.body).toEqual(body);
    req.flush(VIEW);
    await expect(result).resolves.toEqual(VIEW);
  }

  it('options GETs /api/interview-options', async () => {
    const options: InterviewOptions = {
      languages: [{ code: 'en', label: 'English' }],
      levels: ['junior', 'mid-level', 'senior', 'expert'],
    };
    const result = firstValueFrom(api.options());

    const req = httpTesting.expectOne('/api/interview-options');
    expect(req.request.method).toBe('GET');
    req.flush(options);

    await expect(result).resolves.toEqual(options);
  });

  it('start POSTs resume, language and level to /api/sessions', async () => {
    await expectViewCall(api.start('res-1', 'en', 'mid-level'), 'POST', '/api/sessions', {
      resume_id: 'res-1',
      language: 'en',
      interview_level: 'mid-level',
    });
  });

  it('start sends a null level when none is chosen', async () => {
    await expectViewCall(api.start('res-1', 'en', null), 'POST', '/api/sessions', {
      resume_id: 'res-1',
      language: 'en',
      interview_level: null,
    });
  });

  it('list GETs /api/sessions', async () => {
    const summaries: SessionSummary[] = [
      {
        id: SESSION_ID,
        created_at: '2026-10-01T10:00:00Z',
        status: 'completed',
        resume_name: null,
        required_skills: ['Python'],
        completed_at: '2026-10-01T11:00:00Z',
      },
    ];
    const result = firstValueFrom(api.list());

    const req = httpTesting.expectOne('/api/sessions');
    expect(req.request.method).toBe('GET');
    req.flush(summaries);

    await expect(result).resolves.toEqual(summaries);
  });

  it('get GETs /api/sessions/{id}', async () => {
    await expectViewCall(api.get(SESSION_ID), 'GET', BASE, null);
  });

  it('get encodes the session id in the path', async () => {
    const result = firstValueFrom(api.get('a/b'));
    httpTesting.expectOne('/api/sessions/a%2Fb').flush(VIEW);
    await expect(result).resolves.toEqual(VIEW);
  });

  it('cancel POSTs /api/sessions/{id}/cancel', async () => {
    await expectViewCall(api.cancel(SESSION_ID), 'POST', `${BASE}/cancel`, null);
  });

  it('delete sends DELETE /api/sessions/{id} and completes on 204', async () => {
    const result = firstValueFrom(api.delete(SESSION_ID));

    const req = httpTesting.expectOne(BASE);
    expect(req.request.method).toBe('DELETE');
    req.flush(null, { status: 204, statusText: 'No Content' });

    await expect(result).resolves.toBeUndefined();
  });

  it('sendRequirements POSTs the text to /api/sessions/{id}/requirements', async () => {
    await expectViewCall(
      api.sendRequirements(SESSION_ID, 'Python, SQL'),
      'POST',
      `${BASE}/requirements`,
      { text: 'Python, SQL' },
    );
  });

  it('replaceRequirementList PUTs the items to /api/sessions/{id}/requirement-list', async () => {
    const newItem = {
      id: null,
      name: 'SQL',
      original_terms: [],
      classification: 'nice_to_have' as const,
      level: null,
    };
    await expectViewCall(
      api.replaceRequirementList(SESSION_ID, [ITEM, newItem]),
      'PUT',
      `${BASE}/requirement-list`,
      { items: [ITEM, newItem] },
    );
  });

  it('confirmList POSTs /api/sessions/{id}/requirement-list/confirm', async () => {
    await expectViewCall(
      api.confirmList(SESSION_ID),
      'POST',
      `${BASE}/requirement-list/confirm`,
      null,
    );
  });

  it('confirmPlan POSTs /api/sessions/{id}/plan/confirm', async () => {
    await expectViewCall(api.confirmPlan(SESSION_ID), 'POST', `${BASE}/plan/confirm`, null);
  });

  it('retryPreparation POSTs /api/sessions/{id}/preparation/retry', async () => {
    await expectViewCall(
      api.retryPreparation(SESSION_ID),
      'POST',
      `${BASE}/preparation/retry`,
      null,
    );
  });

  it('answer POSTs the answer with the Idempotency-Key header equal to the argument', async () => {
    const result = firstValueFrom(api.answer(SESSION_ID, 'q-1', 'A lazy iterator.', 'key-123'));

    const req = httpTesting.expectOne(`${BASE}/answers`);
    expect(req.request.method).toBe('POST');
    expect(req.request.headers.get('Idempotency-Key')).toBe('key-123');
    expect(req.request.body).toEqual({ question_id: 'q-1', content: 'A lazy iterator.' });
    req.flush(VIEW);

    await expect(result).resolves.toEqual(VIEW);
  });

  it('clarify POSTs the text and returns the assistant message', async () => {
    const message: SessionMessage = {
      id: 'm-1',
      role: 'assistant',
      kind: 'clarification_reply',
      content: 'It means a lazy iterator.',
      created_at: '2026-10-01T10:05:00Z',
    };
    const result = firstValueFrom(api.clarify(SESSION_ID, 'What do you mean?'));

    const req = httpTesting.expectOne(`${BASE}/clarifications`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ text: 'What do you mean?' });
    req.flush({ message });

    await expect(result).resolves.toEqual(message);
  });

  it('retryEvaluation POSTs /api/sessions/{id}/evaluation/retry', async () => {
    await expectViewCall(api.retryEvaluation(SESSION_ID), 'POST', `${BASE}/evaluation/retry`, null);
  });

  it('rejects with a normalized ApiError keeping the details', async () => {
    const result = firstValueFrom(api.start('res-1', 'en', null));

    httpTesting.expectOne('/api/sessions').flush(
      {
        error: {
          code: 'SESSION_IN_PROGRESS',
          message: 'You already have an interview in progress.',
          details: { session_id: SESSION_ID },
        },
      },
      { status: 409, statusText: 'Conflict' },
    );

    await expect(result).rejects.toEqual({
      code: 'SESSION_IN_PROGRESS',
      message: 'You already have an interview in progress.',
      details: { session_id: SESSION_ID },
    } satisfies ApiError);
  });
});
