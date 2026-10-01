import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, ParamMap, convertToParamMap, provideRouter } from '@angular/router';
import { BehaviorSubject, Observable, of, throwError } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ComparePage, NOT_COMPARABLE_MESSAGE, SELECT_TWO_MESSAGE } from './compare-page';
import { Comparison, ReportApi } from './report-api';

const NOT_FOUND = 'Not found.';
const NOT_AVAILABLE = 'The report is not available for this interview.';

function makeComparison(overrides: Partial<Comparison> = {}): Comparison {
  return {
    a: { session_id: 's-a', percentage: '62.5', rubric_version: 'v1', model_version: 'm1' },
    b: { session_id: 's-b', percentage: '80.0', rubric_version: 'v1', model_version: 'm1' },
    common_skills: [
      { skill: 'Python', a_average: '2.5', b_average: '3.0' },
      { skill: 'SQL', a_average: '1.0', b_average: '4.0' },
    ],
    comparable: true,
    differences: [],
    ...overrides,
  };
}

class ReportApiStub {
  get = vi.fn();
  pdfUrl = vi.fn();
  compare = vi.fn<(a: string, b: string) => Observable<Comparison>>(() => of(makeComparison()));
}

function apiError(code: string): ApiError {
  return { code, message: 'raw server text' };
}

describe('ComparePage', () => {
  let fixture: ComponentFixture<ComparePage>;
  let api: ReportApiStub;
  let query: BehaviorSubject<ParamMap>;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const bodyRows = (): HTMLTableRowElement[] =>
    Array.from(root.querySelectorAll<HTMLTableRowElement>('[data-testid="skills-table"] tbody tr'));

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(ComparePage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = new ReportApiStub();
    query = new BehaviorSubject(convertToParamMap({ a: 's-a', b: 's-b' }));
    await TestBed.configureTestingModule({
      imports: [ComparePage],
      providers: [
        provideRouter([]),
        { provide: ReportApi, useValue: api },
        { provide: ActivatedRoute, useValue: { queryParamMap: query.asObservable() } },
      ],
    }).compileComponents();
  });

  afterEach(() => {
    fixture?.destroy();
    root?.remove();
  });

  it('requests the comparison with a and b from the URL and follows query changes', async () => {
    await render();
    expect(api.compare).toHaveBeenCalledWith('s-a', 's-b');

    query.next(convertToParamMap({ a: 's-c', b: 's-d' }));
    fixture.detectChanges();
    expect(api.compare).toHaveBeenLastCalledWith('s-c', 's-d');
    expect(api.compare).toHaveBeenCalledTimes(2);
  });

  it('shows both percentages exactly as received', async () => {
    await render();
    expect(text()).toContain('62.5%');
    expect(text()).toContain('80.0%');
  });

  it('has one table row per common skill with both averages', async () => {
    await render();
    const rows = bodyRows();
    expect(rows).toHaveLength(2);
    const cells = rows.map((row) =>
      Array.from(row.querySelectorAll('th, td')).map((c) => c.textContent?.trim()),
    );
    expect(cells).toEqual([
      ['Python', '2.5', '3.0'],
      ['SQL', '1.0', '4.0'],
    ]);
  });

  it('shows an empty message when there are no common skills', async () => {
    api.compare.mockReturnValue(of(makeComparison({ common_skills: [] })));
    await render();
    expect(bodyRows()).toHaveLength(0);
    expect(text()).toContain('These interviews have no skills in common.');
  });

  it('shows the warning and the differences in the received order when comparable is false', async () => {
    api.compare.mockReturnValue(
      of(
        makeComparison({
          comparable: false,
          differences: ['requirements', 'questions', 'model_version', 'rubric_version'],
        }),
      ),
    );
    await render();
    const warning = root.querySelector<HTMLElement>('[data-testid="not-comparable"]');
    expect(warning).not.toBeNull();
    expect(warning?.textContent).toContain(NOT_COMPARABLE_MESSAGE);
    const items = Array.from(warning?.querySelectorAll('li') ?? []).map((li) =>
      li.textContent?.trim(),
    );
    expect(items).toEqual([
      'Confirmed requirements',
      'Questions',
      'Model version',
      'Rubric version',
    ]);
  });

  it('shows an unknown difference label as plain text', async () => {
    api.compare.mockReturnValue(
      of(makeComparison({ comparable: false, differences: ['<b>other</b>'] })),
    );
    await render();
    const warning = root.querySelector<HTMLElement>('[data-testid="not-comparable"]');
    expect(warning?.textContent).toContain('<b>other</b>');
    expect(warning?.querySelector('b')).toBeNull();
  });

  it('does not show the warning when comparable is true', async () => {
    await render();
    expect(root.querySelector('[data-testid="not-comparable"]')).toBeNull();
    expect(text()).not.toContain(NOT_COMPARABLE_MESSAGE);
  });

  it('links each side to its report', async () => {
    await render();
    const hrefs = Array.from(root.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/sessions/s-a/report');
    expect(hrefs).toContain('/sessions/s-b/report');
  });

  it('does not call the API and asks to select two interviews when a or b is missing', async () => {
    query.next(convertToParamMap({ a: 's-a' }));
    await render();
    expect(api.compare).not.toHaveBeenCalled();
    expect(text()).toContain(SELECT_TWO_MESSAGE);
    const hrefs = Array.from(root.querySelectorAll('a')).map((a) => a.getAttribute('href'));
    expect(hrefs).toContain('/history');
  });

  it.each([
    ['RESOURCE_NOT_FOUND', NOT_FOUND],
    ['REPORT_NOT_AVAILABLE', NOT_AVAILABLE],
    ['VALIDATION_ERROR', SELECT_TWO_MESSAGE],
    [NETWORK_ERROR, 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text, never the server message', async (code, message) => {
    api.compare.mockReturnValue(throwError(() => apiError(code)));
    await render();
    const alert = root.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain(message);
    expect(text()).not.toContain('raw server text');
  });

  it('retries after an error', async () => {
    api.compare.mockReturnValueOnce(throwError(() => apiError(NETWORK_ERROR)));
    await render();
    const retry = Array.from(root.querySelectorAll('button')).find(
      (b) => b.textContent?.trim() === 'Try again',
    );
    retry?.click();
    fixture.detectChanges();
    expect(api.compare).toHaveBeenCalledTimes(2);
    expect(text()).toContain('62.5%');
  });

  it('shows a loading status while the request is pending', async () => {
    api.compare.mockReturnValue(new Observable<Comparison>(() => undefined));
    await render();
    expect(root.querySelector('[role="status"]')?.textContent).toContain('Loading comparison');
  });
});
