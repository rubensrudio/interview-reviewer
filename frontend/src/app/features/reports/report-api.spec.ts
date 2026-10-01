import { HttpErrorResponse, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { Comparison, ReportApi, ReportContent } from './report-api';

const SESSION_ID = 's-1';

const REPORT: ReportContent = {
  summary: 'You answered 2 questions.',
  adherence_percentage: '62.5',
  disclaimer: 'This percentage reflects your answers in this session only.',
  skills: [{ skill: 'Python', average: '2.5', question_positions: [1, 2] }],
  items: [
    {
      position: 1,
      skill: 'Python',
      question: 'What is a generator?',
      answer: 'A lazy iterator.',
      score: 3,
      justification: 'Correct core idea.',
      evidence_quotes: ['lazy iterator'],
      satisfactory: true,
      gap_explanation: null,
      reference_answer: {
        text: 'A function that yields values lazily.',
        points: ['yield', 'lazy evaluation'],
        sources: [
          {
            url: 'https://docs.python.org/3/glossary.html#term-generator',
            title: 'Glossary',
            collected_at: '2026-09-01T00:00:00Z',
            excerpt: 'A function which returns a generator iterator.',
          },
        ],
        hypothetical_example: false,
        example_text: null,
      },
      no_verified_source: false,
    },
  ],
  unsatisfactory_items: [2],
  non_evaluated: { nice_to_have: ['Go'], non_technical: ['English'] },
  plan: { planned_count: 2, skills: ['Python'] },
  model_version: 'qwen2.5:7b-instruct',
  rubric_version: 'v1',
  sources_used: [],
  completed_at: '2026-10-01T10:00:00Z',
};

const COMPARISON: Comparison = {
  a: { session_id: 'a-1', percentage: '62.5', rubric_version: 'v1', model_version: 'm1' },
  b: { session_id: 'b-2', percentage: '75.0', rubric_version: 'v1', model_version: 'm2' },
  common_skills: [{ skill: 'Python', a_average: '2.5', b_average: '3.0' }],
  comparable: false,
  differences: ['model_version'],
};

const NOT_AVAILABLE = {
  error: { code: 'REPORT_NOT_AVAILABLE', message: 'Report not available.' },
};

describe('ReportApi', () => {
  let api: ReportApi;
  let httpTesting: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    api = TestBed.inject(ReportApi);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  it('get GETs /api/sessions/{id}/report and returns the content untouched', async () => {
    const result = firstValueFrom(api.get(SESSION_ID));

    const req = httpTesting.expectOne(`/api/sessions/${SESSION_ID}/report`);
    expect(req.request.method).toBe('GET');
    req.flush(REPORT);

    await expect(result).resolves.toEqual(REPORT);
  });

  it('get encodes the session id in the path', async () => {
    const result = firstValueFrom(api.get('a/b'));

    httpTesting.expectOne('/api/sessions/a%2Fb/report').flush(REPORT);

    await expect(result).resolves.toEqual(REPORT);
  });

  it('get surfaces failures as ApiError', async () => {
    const result = firstValueFrom(api.get(SESSION_ID));

    httpTesting
      .expectOne(`/api/sessions/${SESSION_ID}/report`)
      .flush(NOT_AVAILABLE, { status: 409, statusText: 'Conflict' });

    const error = await result.catch((err: unknown) => err);
    expect(error).not.toBeInstanceOf(HttpErrorResponse);
    expect((error as ApiError).code).toBe(NOT_AVAILABLE.error.code);
    expect((error as ApiError).message).toBe(NOT_AVAILABLE.error.message);
  });

  it('pdfUrl points to /api/sessions/{id}/report.pdf without a request', () => {
    expect(api.pdfUrl(SESSION_ID)).toBe(`/api/sessions/${SESSION_ID}/report.pdf`);
    expect(api.pdfUrl('a/b')).toBe('/api/sessions/a%2Fb/report.pdf');
    httpTesting.expectNone(() => true);
  });

  it('compare GETs /api/reports/compare?a=..&b=..', async () => {
    const result = firstValueFrom(api.compare('a-1', 'b-2'));

    const req = httpTesting.expectOne(
      (r) => r.url === '/api/reports/compare' && r.method === 'GET',
    );
    expect(req.request.params.get('a')).toBe('a-1');
    expect(req.request.params.get('b')).toBe('b-2');
    expect(req.request.urlWithParams).toBe('/api/reports/compare?a=a-1&b=b-2');
    req.flush(COMPARISON);

    await expect(result).resolves.toEqual(COMPARISON);
  });

  it('compare surfaces failures as ApiError', async () => {
    const result = firstValueFrom(api.compare('a-1', 'b-2'));

    httpTesting
      .expectOne((r) => r.url === '/api/reports/compare')
      .flush(NOT_AVAILABLE, { status: 409, statusText: 'Conflict' });

    const error = await result.catch((err: unknown) => err);
    expect((error as ApiError).code).toBe(NOT_AVAILABLE.error.code);
  });
});
