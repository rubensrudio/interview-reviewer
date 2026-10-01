import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import {
  RequirementItem,
  RequirementItemInput,
  SessionApi,
  SessionMessage,
  SessionView,
} from './session-api';
import { SessionPage } from './session-page';

const PREPARATION_FAILED =
  "We couldn't prepare your questions. Your requirements are saved — try again.";
const EVALUATING =
  'Evaluating your answers. You can leave this page; the report will appear in your history.';
const EVALUATION_FAILED =
  "We couldn't finish evaluating your answers. Your answers are saved — try again.";
const CANCEL_MESSAGE =
  'Cancel this interview? It cannot be resumed and no report will be generated.';
const EMPTY_REQUIREMENTS =
  'Please paste the job requirements for the position you are preparing for.';

function makeSession(overrides: Partial<SessionView> = {}): SessionView {
  return {
    id: 's-1',
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

function makeMessage(overrides: Partial<SessionMessage> = {}): SessionMessage {
  return {
    id: 'm-1',
    role: 'assistant',
    kind: 'info',
    content: EMPTY_REQUIREMENTS,
    created_at: '2026-03-10T12:00:00Z',
    ...overrides,
  };
}

function makeItem(overrides: Partial<RequirementItem> = {}): RequirementItem {
  return {
    id: 'req-1',
    name: 'Python',
    original_terms: ['Python'],
    classification: 'required',
    level: 'senior',
    pending_clarification: false,
    clarification_question: null,
    ...overrides,
  };
}

function apiError(code: string, details?: Record<string, unknown>): ApiError {
  return details
    ? { code, message: 'raw server text', details }
    : { code, message: 'raw server text' };
}

class SessionApiStub {
  get = vi.fn<(id: string) => Observable<SessionView>>(() => of(makeSession()));
  cancel = vi.fn<(id: string) => Observable<SessionView>>(() =>
    of(makeSession({ status: 'cancelled' })),
  );
  sendRequirements = vi.fn<(id: string, text: string) => Observable<SessionView>>();
  replaceRequirementList =
    vi.fn<
      (
        id: string,
        items: readonly (RequirementItem | RequirementItemInput)[],
      ) => Observable<SessionView>
    >();
  confirmList = vi.fn<(id: string) => Observable<SessionView>>();
  confirmPlan = vi.fn<(id: string) => Observable<SessionView>>();
  retryPreparation = vi.fn<(id: string) => Observable<SessionView>>();
  retryEvaluation = vi.fn<(id: string) => Observable<SessionView>>();
  answer = vi.fn();
  clarify = vi.fn();
}

describe('SessionPage', () => {
  let fixture: ComponentFixture<SessionPage>;
  let api: SessionApiStub;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const hasButton = (label: string): boolean =>
    Array.from(root.querySelectorAll('button')).some((b) => b.textContent?.trim() === label);
  const fieldByLabel = (label: string): HTMLTextAreaElement => {
    const lbl = Array.from(root.querySelectorAll('label')).find(
      (l) => l.textContent?.trim() === label,
    );
    const id = lbl?.getAttribute('for');
    const control = id ? root.querySelector<HTMLTextAreaElement>(`#${id}`) : null;
    if (!control) {
      throw new Error(`Field "${label}" not found`);
    }
    return control;
  };

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(SessionPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    await settle();
  }

  function type(control: HTMLTextAreaElement, value: string): void {
    control.value = value;
    control.dispatchEvent(new Event('input'));
  }

  beforeEach(async () => {
    api = new SessionApiStub();
    await TestBed.configureTestingModule({
      imports: [SessionPage],
      providers: [
        provideRouter([]),
        { provide: SessionApi, useValue: api },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: convertToParamMap({ id: 's-1' }) } },
        },
      ],
    }).compileComponents();
  });

  afterEach(() => {
    vi.useRealTimers();
    fixture?.destroy();
    root?.remove();
  });

  it('loads the session from the route id', async () => {
    await render();
    expect(api.get).toHaveBeenCalledWith('s-1');
  });

  it('shows "Not found." for an unknown or foreign session', async () => {
    api.get.mockReturnValue(throwError(() => apiError('RESOURCE_NOT_FOUND')));
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('Not found.');
    expect(text()).not.toContain('raw server text');
  });

  describe('collecting requirements', () => {
    it('renders the chat messages and sends the requirements', async () => {
      api.get.mockReturnValue(of(makeSession({ messages: [makeMessage()] })));
      api.sendRequirements.mockReturnValue(
        of(
          makeSession({
            status: 'awaiting_confirmation',
            requirements: { items: [makeItem()], non_technical: [] },
          }),
        ),
      );
      await render();

      expect(text()).toContain(EMPTY_REQUIREMENTS);
      type(fieldByLabel('Job requirements'), 'Senior Python developer');
      await settle();
      buttonByText('Send').click();
      await settle();

      expect(api.sendRequirements).toHaveBeenCalledWith('s-1', 'Senior Python developer');
      expect(root.querySelector('app-requirement-list-editor')).not.toBeNull();
    });

    it('rejects a blank message without calling the API (PLAN-90)', async () => {
      await render();
      type(fieldByLabel('Job requirements'), '   ');
      await settle();
      buttonByText('Send').click();
      await settle();

      expect(api.sendRequirements).not.toHaveBeenCalled();
      expect(root.querySelector('[role="alert"]')?.textContent).toContain(EMPTY_REQUIREMENTS);
    });

    it('shows the catalog text when the server rejects the requirements', async () => {
      api.sendRequirements.mockReturnValue(throwError(() => apiError('LLM_UNAVAILABLE')));
      await render();
      type(fieldByLabel('Job requirements'), 'Python');
      await settle();
      buttonByText('Send').click();
      await settle();

      expect(root.querySelector('[role="alert"]')?.textContent).toContain(
        'The assistant is temporarily unavailable. Please try again.',
      );
      expect(text()).not.toContain('raw server text');
      expect(fieldByLabel('Job requirements').value).toBe('Python');
    });
  });

  describe('awaiting confirmation', () => {
    const awaiting = (): SessionView =>
      makeSession({
        status: 'awaiting_confirmation',
        requirements: { items: [makeItem()], non_technical: [] },
      });

    it('renders the editor instead of the requirements field', async () => {
      api.get.mockReturnValue(of(awaiting()));
      await render();

      expect(root.querySelector('app-requirement-list-editor')).not.toBeNull();
      expect(root.querySelector('textarea')).toBeNull();
    });

    it('confirms the list and applies the returned proposal', async () => {
      api.get.mockReturnValue(of(awaiting()));
      api.confirmList.mockReturnValue(
        of({ ...awaiting(), proposal: { planned_count: 5, skills: ['Python'] } }),
      );
      await render();

      const page = fixture.componentInstance as unknown as { onConfirmList(): void };
      page.onConfirmList();
      await settle();

      expect(api.confirmList).toHaveBeenCalledWith('s-1');
      expect(text()).toContain('5 questions');
    });

    it('persists list edits with replaceRequirementList', async () => {
      api.get.mockReturnValue(of(awaiting()));
      api.replaceRequirementList.mockReturnValue(of(awaiting()));
      await render();

      const items = [makeItem({ name: 'Go' })];
      const page = fixture.componentInstance as unknown as {
        onListChanged(items: RequirementItem[]): void;
      };
      page.onListChanged(items);
      await settle();

      expect(api.replaceRequirementList).toHaveBeenCalledWith('s-1', items);
    });

    it('passes a new error object to the editor on every failure', async () => {
      api.get.mockReturnValue(of(awaiting()));
      api.confirmList.mockReturnValue(throwError(() => apiError('NO_REQUIRED_SKILLS')));
      await render();

      const page = fixture.componentInstance as unknown as {
        onConfirmList(): void;
        editorError(): ApiError | null;
      };
      page.onConfirmList();
      await settle();
      const first = page.editorError();
      page.onConfirmList();
      await settle();
      const second = page.editorError();

      expect(first?.code).toBe('NO_REQUIRED_SKILLS');
      expect(second).not.toBe(first);
      expect(text()).toContain('Define at least one required technical skill to continue.');
    });

    it('never forwards a raw server message to the editor', async () => {
      api.get.mockReturnValue(of(awaiting()));
      api.confirmPlan.mockReturnValue(throwError(() => apiError('HTTP_ERROR')));
      await render();

      const page = fixture.componentInstance as unknown as { onConfirmPlan(): void };
      page.onConfirmPlan();
      await settle();

      expect(text()).not.toContain('raw server text');
      expect(text()).toContain('Something went wrong. Please try again.');
    });
  });

  it('polls every 3 seconds while preparing questions', async () => {
    vi.useFakeTimers();
    api.get.mockReturnValue(of(makeSession({ status: 'preparing_questions' })));
    await render();
    expect(root.querySelector('[role="status"]')).not.toBeNull();

    api.get.mockReturnValue(of(makeSession({ status: 'preparation_failed' })));
    await vi.advanceTimersByTimeAsync(3000);
    await settle();

    expect(api.get).toHaveBeenCalledTimes(2);
    expect(text()).toContain(PREPARATION_FAILED);
  });

  it('shows the preparation failure and "Try again" calls retryPreparation', async () => {
    api.get.mockReturnValue(of(makeSession({ status: 'preparation_failed' })));
    api.retryPreparation.mockReturnValue(of(makeSession({ status: 'preparing_questions' })));
    await render();

    expect(text()).toContain(PREPARATION_FAILED);
    buttonByText('Try again').click();
    await settle();

    expect(api.retryPreparation).toHaveBeenCalledWith('s-1');
    expect(text()).not.toContain(PREPARATION_FAILED);
  });

  it('shows the evaluating message and polls until the report is ready', async () => {
    vi.useFakeTimers();
    api.get.mockReturnValue(of(makeSession({ status: 'evaluating' })));
    await render();

    expect(text()).toContain(EVALUATING);
    expect(root.querySelector('textarea')).toBeNull();

    api.get.mockReturnValue(of(makeSession({ status: 'completed', report_available: true })));
    await vi.advanceTimersByTimeAsync(3000);
    await settle();

    expect(api.get).toHaveBeenCalledTimes(2);
    const link = root.querySelector<HTMLAnchorElement>('a[href="/sessions/s-1/report"]');
    expect(link?.textContent?.trim()).toBe('View report');

    await vi.advanceTimersByTimeAsync(6000);
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('shows the evaluation failure and "Try again" calls retryEvaluation', async () => {
    api.get.mockReturnValue(of(makeSession({ status: 'evaluation_failed' })));
    api.retryEvaluation.mockReturnValue(of(makeSession({ status: 'evaluating' })));
    await render();

    expect(text()).toContain(EVALUATION_FAILED);
    expect(text()).not.toContain('View report');
    buttonByText('Try again').click();
    await settle();

    expect(api.retryEvaluation).toHaveBeenCalledWith('s-1');
    expect(text()).toContain(EVALUATING);
  });

  it('reloads the session when a retry is no longer allowed', async () => {
    api.get.mockReturnValue(of(makeSession({ status: 'evaluation_failed' })));
    api.retryEvaluation.mockReturnValue(throwError(() => apiError('INVALID_STATE')));
    await render();

    api.get.mockReturnValue(of(makeSession({ status: 'expired' })));
    buttonByText('Try again').click();
    await settle();

    expect(api.get).toHaveBeenCalledTimes(2);
    expect(text()).toContain('This action is not available at this step.');
    expect(text()).toContain('Expired');
  });

  describe('interview', () => {
    const interviewing = (): SessionView =>
      makeSession({
        status: 'in_interview',
        counter: { planned: 3, answered: 0, remaining: 3 },
        current_question: { id: 'q-1', position: 1, skill: 'Python', text: 'What is a GIL?' },
        messages: [
          makeMessage({ id: 'm-0', kind: 'requirements', role: 'candidate', content: 'Old JD' }),
          makeMessage({
            id: 'm-1',
            kind: 'clarification_request',
            role: 'candidate',
            content: 'Which Python version?',
          }),
          makeMessage({ id: 'm-2', kind: 'clarification_reply', content: 'Any recent one.' }),
        ],
      });

    it('renders the panel and the clarification messages (INTV-08)', async () => {
      api.get.mockReturnValue(of(interviewing()));
      await render();

      expect(root.querySelector('app-interview-panel')).not.toBeNull();
      expect(text()).toContain('Which Python version?');
      expect(text()).toContain('Any recent one.');
      expect(text()).not.toContain('Old JD');
    });

    it('applies the view emitted by the panel', async () => {
      api.get.mockReturnValue(of(interviewing()));
      await render();

      const page = fixture.componentInstance as unknown as { applyView(view: SessionView): void };
      page.applyView(makeSession({ status: 'evaluating' }));
      await settle();

      expect(root.querySelector('app-interview-panel')).toBeNull();
      expect(text()).toContain(EVALUATING);
    });
  });

  it('only cancels after the candidate confirms in the dialog', async () => {
    api.get.mockReturnValue(of(makeSession({ status: 'preparation_failed' })));
    await render();

    buttonByText('Cancel interview').click();
    await settle();
    const dialog = root.querySelector('dialog');
    expect(dialog?.textContent).toContain(CANCEL_MESSAGE);
    expect(api.cancel).not.toHaveBeenCalled();

    buttonByText('Keep interview', dialog as HTMLElement).click();
    await settle();
    expect(root.querySelector('dialog')).toBeNull();
    expect(api.cancel).not.toHaveBeenCalled();

    buttonByText('Cancel interview').click();
    await settle();
    buttonByText('Cancel interview', root.querySelector('dialog') as HTMLElement).click();
    await settle();

    expect(api.cancel).toHaveBeenCalledTimes(1);
    expect(api.cancel).toHaveBeenCalledWith('s-1');
    expect(text()).toContain('Cancelled');
    expect(hasButton('Cancel interview')).toBe(false);
  });

  it('shows an expired session read only, without input fields', async () => {
    api.get.mockReturnValue(
      of(
        makeSession({
          status: 'expired',
          messages: [makeMessage()],
          answered: [
            { question_id: 'q-1', position: 1, skill: 'Python', question: 'Q?', answer: 'A.' },
          ],
        }),
      ),
    );
    await render();

    expect(text()).toContain('Expired');
    expect(text()).toContain('This interview is closed.');
    expect(text()).toContain('A.');
    expect(root.querySelectorAll('textarea, input, select')).toHaveLength(0);
    expect(hasButton('Cancel interview')).toBe(false);
    expect(hasButton('Try again')).toBe(false);
  });

  it('shows a cancelled session read only', async () => {
    api.get.mockReturnValue(of(makeSession({ status: 'cancelled' })));
    await render();

    expect(text()).toContain('Cancelled');
    expect(root.querySelectorAll('textarea, input, select')).toHaveLength(0);
    expect(root.querySelectorAll('button')).toHaveLength(0);
  });
});
