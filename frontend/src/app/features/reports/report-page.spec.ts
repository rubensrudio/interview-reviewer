import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, ParamMap, convertToParamMap } from '@angular/router';
import { BehaviorSubject, Observable, of, throwError } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ReferenceAnswer, ReportApi, ReportContent, ReportItem, SourceRef } from './report-api';
import { ReportPage } from './report-page';

const DISCLAIMER =
  'This percentage reflects your answers in this session only. It is not a hiring prediction or a certification of professional competence.';
const NOT_AVAILABLE = 'The report is not available for this interview.';

function makeSource(overrides: Partial<SourceRef> = {}): SourceRef {
  return {
    url: 'https://docs.python.org/3/glossary.html#term-generator',
    title: 'Python glossary',
    collected_at: '2026-09-01T00:00:00Z',
    excerpt: 'A function which returns a generator iterator.',
    ...overrides,
  };
}

function makeItem(overrides: Partial<ReportItem> = {}): ReportItem {
  return {
    position: 1,
    skill: 'Python',
    question: 'What is a generator?',
    answer: 'A lazy iterator.',
    score: 4,
    justification: 'Correct and complete.',
    evidence_quotes: ['lazy iterator'],
    satisfactory: true,
    gap_explanation: null,
    reference_answer: null,
    no_verified_source: false,
    ...overrides,
  };
}

function makeReference(overrides: Partial<ReferenceAnswer> = {}): ReferenceAnswer {
  return {
    text: 'The GIL lets one thread run Python bytecode at a time.',
    points: ['one thread at a time', 'I/O releases the GIL'],
    sources: [makeSource()],
    hypothetical_example: false,
    example_text: null,
    ...overrides,
  };
}

function makeUnsatisfactory(overrides: Partial<ReportItem> = {}): ReportItem {
  return makeItem({
    position: 2,
    question: 'How does the GIL affect threads?',
    answer: "I don't know.",
    score: 0,
    justification: 'No answer given.',
    evidence_quotes: [],
    satisfactory: false,
    gap_explanation: 'The answer does not explain the GIL.',
    reference_answer: makeReference(),
    ...overrides,
  });
}

function makeReport(overrides: Partial<ReportContent> = {}): ReportContent {
  return {
    summary: 'You answered 2 questions; 1 was unsatisfactory.',
    adherence_percentage: '62.5',
    disclaimer: DISCLAIMER,
    skills: [{ skill: 'Python', average: '2.0', question_positions: [1, 2] }],
    items: [makeItem(), makeUnsatisfactory()],
    unsatisfactory_items: [2],
    non_evaluated: { nice_to_have: ['Go'], non_technical: ['Fluent English'] },
    plan: { planned_count: 2, skills: ['Python'] },
    model_version: 'qwen2.5:7b-instruct',
    rubric_version: 'v1',
    sources_used: [makeSource()],
    completed_at: '2026-10-01T10:00:00Z',
    ...overrides,
  };
}

class ReportApiStub {
  get = vi.fn<(id: string) => Observable<ReportContent>>(() => of(makeReport()));
  pdfUrl = vi.fn<(id: string) => string>(
    (id) => `/api/sessions/${encodeURIComponent(id)}/report.pdf`,
  );
  compare = vi.fn();
}

function apiError(code: string): ApiError {
  return { code, message: 'raw server text' };
}

describe('ReportPage', () => {
  let fixture: ComponentFixture<ReportPage>;
  let api: ReportApiStub;
  let params: BehaviorSubject<ParamMap>;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const section = (title: string): HTMLElement => {
    const heading = Array.from(root.querySelectorAll('h2')).find(
      (h) => h.textContent?.trim() === title,
    );
    const found = heading?.closest('section');
    if (!found) {
      throw new Error(`Section "${title}" not found`);
    }
    return found;
  };
  const linkByText = (label: string): HTMLAnchorElement => {
    const found = Array.from(root.querySelectorAll('a')).find(
      (a) => a.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Link "${label}" not found`);
    }
    return found;
  };

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(ReportPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = new ReportApiStub();
    params = new BehaviorSubject(convertToParamMap({ id: 's-1' }));
    await TestBed.configureTestingModule({
      imports: [ReportPage],
      providers: [
        { provide: ReportApi, useValue: api },
        { provide: ActivatedRoute, useValue: { paramMap: params.asObservable() } },
      ],
    }).compileComponents();
  });

  afterEach(() => {
    fixture?.destroy();
    root?.remove();
  });

  it('loads the report from the route id and follows route changes', async () => {
    await render();
    expect(api.get).toHaveBeenCalledWith('s-1');

    params.next(convertToParamMap({ id: 's-2' }));
    fixture.detectChanges();
    expect(api.get).toHaveBeenLastCalledWith('s-2');
  });

  it('shows "62.5" as "62.5%" and the section 9 disclaimer', async () => {
    await render();
    expect(text()).toContain('62.5%');
    expect(text()).toContain(DISCLAIMER);
  });

  it('shows the percentage exactly as received, without recalculating it', async () => {
    api.get.mockReturnValue(of(makeReport({ adherence_percentage: '0.0' })));
    await render();
    expect(text()).toContain('0.0%');
    expect(text()).not.toContain('62.5%');
  });

  it('renders the summary, skill performance and every question with answer, score and justification', async () => {
    await render();
    expect(text()).toContain('You answered 2 questions; 1 was unsatisfactory.');
    const skills = section('Performance by skill');
    expect(skills.textContent).toContain('Python');
    expect(skills.textContent).toContain('2.0');

    const questions = section('Questions and answers');
    expect(questions.textContent).toContain('What is a generator?');
    expect(questions.textContent).toContain('A lazy iterator.');
    expect(questions.textContent).toContain('4');
    expect(questions.textContent).toContain('Correct and complete.');
    expect(questions.textContent).toContain('How does the GIL affect threads?');
  });

  it('lists the satisfactory points', async () => {
    await render();
    const satisfactory = section('Satisfactory points');
    expect(satisfactory.textContent).toContain('What is a generator?');
    expect(satisfactory.textContent).not.toContain('How does the GIL affect threads?');
  });

  it('shows every unsatisfactory item with the gap, reference answer and sources', async () => {
    await render();
    const unsatisfactory = section('Unsatisfactory items');
    expect(unsatisfactory.textContent).toContain('How does the GIL affect threads?');
    expect(unsatisfactory.textContent).toContain('The answer does not explain the GIL.');
    expect(unsatisfactory.textContent).toContain(
      'The GIL lets one thread run Python bytecode at a time.',
    );
    expect(unsatisfactory.textContent).toContain('I/O releases the GIL');
    const link = unsatisfactory.querySelector<HTMLAnchorElement>('a[href^="https://"]');
    expect(link?.getAttribute('href')).toBe(
      'https://docs.python.org/3/glossary.html#term-generator',
    );
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(link?.textContent).toContain('Python glossary');
  });

  it('labels a hypothetical example as "Hypothetical example"', async () => {
    api.get.mockReturnValue(
      of(
        makeReport({
          items: [
            makeItem(),
            makeUnsatisfactory({
              reference_answer: makeReference({
                hypothetical_example: true,
                example_text: 'At a fintech, you moved CPU-bound work to processes.',
              }),
            }),
          ],
        }),
      ),
    );
    await render();
    const unsatisfactory = section('Unsatisfactory items');
    expect(unsatisfactory.textContent).toContain('Hypothetical example');
    expect(unsatisfactory.textContent).toContain(
      'At a fintech, you moved CPU-bound work to processes.',
    );
  });

  it('does not show the hypothetical label for a non-hypothetical reference', async () => {
    await render();
    expect(text()).not.toContain('Hypothetical example');
  });

  it('marks a "no verified source" item and shows no source for it', async () => {
    api.get.mockReturnValue(
      of(
        makeReport({
          items: [
            makeUnsatisfactory({
              position: 1,
              no_verified_source: true,
              reference_answer: makeReference({ sources: [] }),
            }),
          ],
          unsatisfactory_items: [1],
          sources_used: [],
        }),
      ),
    );
    await render();
    const unsatisfactory = section('Unsatisfactory items');
    expect(unsatisfactory.textContent).toContain('No verified source');
    expect(unsatisfactory.querySelectorAll('a').length).toBe(0);
    expect(text()).not.toContain('Python glossary');
  });

  it('shows that there are no unsatisfactory items when none exist', async () => {
    api.get.mockReturnValue(
      of(
        makeReport({
          adherence_percentage: '100.0',
          items: [makeItem()],
          unsatisfactory_items: [],
        }),
      ),
    );
    await render();
    expect(section('Unsatisfactory items').textContent).toContain('No unsatisfactory items.');
  });

  it('lists the knowledge sources with title, collection date and excerpt', async () => {
    await render();
    const sources = section('Sources');
    expect(sources.textContent).toContain('Python glossary');
    expect(sources.textContent).toContain('A function which returns a generator iterator.');
    expect(sources.textContent).toContain('2026');
  });

  it('renders a non-http source URL as plain text, never as a link', async () => {
    api.get.mockReturnValue(
      of(makeReport({ sources_used: [makeSource({ url: 'javascript:alert(1)', title: 'Bad' })] })),
    );
    await render();
    const sources = section('Sources');
    expect(sources.querySelector('a')).toBeNull();
    expect(sources.textContent).toContain('javascript:alert(1)');
  });

  it('lists the requirements not evaluated', async () => {
    await render();
    const notEvaluated = section('Not evaluated in this session');
    expect(notEvaluated.textContent).toContain('Go');
    expect(notEvaluated.textContent).toContain('Fluent English');
  });

  it('links "Download PDF" to /api/sessions/{id}/report.pdf', async () => {
    await render();
    expect(api.pdfUrl).toHaveBeenCalledWith('s-1');
    expect(linkByText('Download PDF').getAttribute('href')).toBe('/api/sessions/s-1/report.pdf');
  });

  it('renders external text by interpolation only', async () => {
    api.get.mockReturnValue(
      of(makeReport({ items: [makeItem({ answer: '<img src=x onerror=alert(1)>' })] })),
    );
    await render();
    expect(root.querySelector('img')).toBeNull();
    expect(text()).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows the catalog message for REPORT_NOT_AVAILABLE (409)', async () => {
    api.get.mockReturnValue(throwError(() => apiError('REPORT_NOT_AVAILABLE')));
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(NOT_AVAILABLE);
    expect(text()).not.toContain('raw server text');
    expect(text()).not.toContain('Download PDF');
  });

  it('shows "Not found." for an unknown or foreign session', async () => {
    api.get.mockReturnValue(throwError(() => apiError('RESOURCE_NOT_FOUND')));
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('Not found.');
  });

  it('shows a network error with a retry that reloads the report', async () => {
    api.get.mockReturnValueOnce(throwError(() => apiError(NETWORK_ERROR)));
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Unable to reach the server.',
    );
    const retry = Array.from(root.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Try again',
    );
    retry?.click();
    fixture.detectChanges();
    expect(api.get).toHaveBeenCalledTimes(2);
    expect(text()).toContain('62.5%');
  });
});
