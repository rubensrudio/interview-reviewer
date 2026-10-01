import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { ResumeApi, ResumeStatus, ResumeSummary } from '../resumes/resume-api';
import { NewSessionPage } from './new-session-page';
import { ExpectedLevel, InterviewOptions, SessionApi, SessionView } from './session-api';

const IN_PROGRESS_MESSAGE =
  'You already have an interview in progress. Resume or cancel it to start a new one.';

function makeResume(overrides: Partial<ResumeSummary> = {}): ResumeSummary {
  return {
    id: 'r-1',
    filename: 'cv.pdf',
    uploaded_at: '2026-03-10T12:00:00Z',
    status: 'ready',
    failure_code: null,
    failure_message: null,
    ...overrides,
  };
}

function makeSession(overrides: Partial<SessionView> = {}): SessionView {
  return {
    id: 's-new',
    status: 'collecting_requirements',
    created_at: '2026-03-10T12:00:00Z',
    language: 'en',
    interview_level: null,
    resume_name: 'cv.pdf',
    messages: [],
    requirements: null,
    proposal: null,
    counter: null,
    current_question: null,
    answered: [],
    report_available: false,
    ...overrides,
  };
}

const OPTIONS: InterviewOptions = {
  languages: [{ code: 'en', label: 'English' }],
  levels: ['junior', 'mid-level', 'senior', 'expert'],
};

class ResumeApiStub {
  list = vi.fn<(status?: ResumeStatus) => Observable<ResumeSummary[]>>(() =>
    of([
      makeResume({ id: 'r-1', filename: 'backend.pdf' }),
      makeResume({ id: 'r-2', filename: 'frontend.pdf' }),
    ]),
  );
}

class SessionApiStub {
  options = vi.fn<() => Observable<InterviewOptions>>(() => of(OPTIONS));
  start = vi.fn<
    (resumeId: string, language: string, level: ExpectedLevel | null) => Observable<SessionView>
  >(() => of(makeSession()));
  cancel = vi.fn<(id: string) => Observable<SessionView>>(() =>
    of(makeSession({ id: 's-old', status: 'cancelled' })),
  );
}

function apiError(code: string, details?: Record<string, unknown>): ApiError {
  return { code, message: 'raw backend message', details };
}

describe('NewSessionPage', () => {
  let fixture: ComponentFixture<NewSessionPage>;
  let resumes: ResumeApiStub;
  let sessions: SessionApiStub;
  let router: Router;
  let navigate: ReturnType<typeof vi.spyOn>;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const select = (id: string): HTMLSelectElement => {
    const found = root.querySelector<HTMLSelectElement>(`#${id}`);
    if (!found) {
      throw new Error(`select #${id} not found`);
    }
    return found;
  };
  const optionTexts = (el: HTMLSelectElement): string[] =>
    Array.from(el.options).map((o) => o.textContent?.trim() ?? '');
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const dialog = (): HTMLElement | null => root.querySelector('[role="alertdialog"]');

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(NewSessionPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    await settle();
  }

  async function choose(id: string, value: string): Promise<void> {
    const el = select(id);
    el.value = value;
    el.dispatchEvent(new Event('change'));
    await settle();
  }

  async function submit(): Promise<void> {
    buttonByText('Start interview').click();
    await settle();
  }

  beforeEach(async () => {
    resumes = new ResumeApiStub();
    sessions = new SessionApiStub();
    await TestBed.configureTestingModule({
      imports: [NewSessionPage],
      providers: [
        provideRouter([]),
        { provide: ResumeApi, useValue: resumes },
        { provide: SessionApi, useValue: sessions },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
  });

  afterEach(() => {
    fixture?.destroy();
    root?.remove();
  });

  it('lists only the resumes returned by list("ready") (CV-11)', async () => {
    await render();

    expect(resumes.list).toHaveBeenCalledWith('ready');
    const texts = optionTexts(select('session-resume'));
    expect(texts).toContain('backend.pdf');
    expect(texts).toContain('frontend.pdf');
    expect(
      select('session-resume').querySelectorAll('option[value="r-1"], option[value="r-2"]'),
    ).toHaveLength(2);
  });

  it('offers the languages and levels from the interview options (LANG-01, LANG-02)', async () => {
    await render();

    expect(optionTexts(select('session-language'))).toEqual(['English']);
    expect(select('session-language').value).toBe('en');
    expect(optionTexts(select('session-level'))).toEqual([
      'No specific level',
      'Junior',
      'Mid-level',
      'Senior',
      'Expert',
    ]);
  });

  it('shows an empty state with a link to the resumes page when none is ready', async () => {
    resumes.list.mockReturnValue(of([]));
    await render();

    expect(text()).toContain('You have no ready resumes yet.');
    expect(root.querySelector('a[href="/resumes"]')).not.toBeNull();
    expect(root.querySelector('#session-resume')).toBeNull();
  });

  it('shows a retryable error when loading fails', async () => {
    sessions.options.mockReturnValueOnce(throwError(() => apiError('NETWORK_ERROR')));
    await render();

    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Unable to reach the server.',
    );
    buttonByText('Try again').click();
    await settle();
    expect(root.querySelector('#session-resume')).not.toBeNull();
  });

  it('requires a resume before starting', async () => {
    await render();
    await submit();

    expect(sessions.start).not.toHaveBeenCalled();
    expect(text()).toContain('Choose a resume.');
    expect(select('session-resume').getAttribute('aria-invalid')).toBe('true');
  });

  it('starts the session and navigates to /sessions/{id} on success (PLAN-01)', async () => {
    await render();
    await choose('session-resume', 'r-2');
    await choose('session-level', 'senior');
    await submit();

    expect(sessions.start).toHaveBeenCalledWith('r-2', 'en', 'senior');
    expect(navigate).toHaveBeenCalledWith(['/sessions', 's-new']);
  });

  it('sends a null level when no level is chosen', async () => {
    await render();
    await choose('session-resume', 'r-1');
    await submit();

    expect(sessions.start).toHaveBeenCalledWith('r-1', 'en', null);
  });

  it('shows the in-progress dialog with Resume and Cancel on 409 SESSION_IN_PROGRESS (PLAN-02)', async () => {
    sessions.start.mockReturnValueOnce(
      throwError(() => apiError('SESSION_IN_PROGRESS', { session_id: 's-old' })),
    );
    await render();
    await choose('session-resume', 'r-1');
    await submit();

    const open = dialog();
    expect(open).not.toBeNull();
    expect(open?.textContent).toContain(IN_PROGRESS_MESSAGE);
    expect(buttonByText('Resume', open as HTMLElement)).toBeTruthy();
    expect(buttonByText('Cancel', open as HTMLElement)).toBeTruthy();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('Resume navigates to the session in progress', async () => {
    sessions.start.mockReturnValueOnce(
      throwError(() => apiError('SESSION_IN_PROGRESS', { session_id: 's-old' })),
    );
    await render();
    await choose('session-resume', 'r-1');
    await submit();

    buttonByText('Resume', dialog() as HTMLElement).click();
    await settle();

    expect(sessions.cancel).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith(['/sessions', 's-old']);
  });

  it('Cancel cancels the session in progress and starts the new one again', async () => {
    sessions.start.mockReturnValueOnce(
      throwError(() => apiError('SESSION_IN_PROGRESS', { session_id: 's-old' })),
    );
    await render();
    await choose('session-resume', 'r-1');
    await submit();

    buttonByText('Cancel', dialog() as HTMLElement).click();
    await settle();

    expect(sessions.cancel).toHaveBeenCalledWith('s-old');
    expect(sessions.start).toHaveBeenCalledTimes(2);
    expect(sessions.start).toHaveBeenLastCalledWith('r-1', 'en', null);
    expect(dialog()).toBeNull();
    expect(navigate).toHaveBeenCalledWith(['/sessions', 's-new']);
  });

  it('maps other start errors to the catalog text, never the raw message', async () => {
    sessions.start.mockReturnValueOnce(throwError(() => apiError('RESUME_NOT_READY')));
    await render();
    await choose('session-resume', 'r-1');
    await submit();

    const alert = root.querySelector('.form-error[role="alert"]');
    expect(alert?.textContent).toContain('This resume is not ready yet.');
    expect(text()).not.toContain('raw backend message');
    expect(navigate).not.toHaveBeenCalled();
  });
});
