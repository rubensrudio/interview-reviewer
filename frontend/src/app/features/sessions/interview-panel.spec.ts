import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Observable, Subject, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { InterviewPanel } from './interview-panel';
import { SessionApi, SessionMessage, SessionView } from './session-api';

function makeView(overrides: Partial<SessionView> = {}): SessionView {
  return {
    id: 'session-1',
    status: 'in_interview',
    created_at: '2026-01-01T00:00:00Z',
    language: 'en',
    interview_level: 'senior',
    resume_name: 'cv.pdf',
    messages: [],
    requirements: null,
    proposal: null,
    counter: { planned: 5, answered: 1, remaining: 4 },
    current_question: {
      id: 'q-2',
      position: 2,
      skill: 'TypeScript',
      text: 'What is a discriminated union?',
    },
    answered: [
      {
        question_id: 'q-1',
        position: 1,
        skill: 'Angular',
        question: 'What is a signal?',
        answer: 'A reactive value.',
      },
    ],
    report_available: false,
    ...overrides,
  };
}

const NEXT_VIEW = makeView({
  counter: { planned: 5, answered: 2, remaining: 3 },
  current_question: { id: 'q-3', position: 3, skill: 'RxJS', text: 'What is a Subject?' },
  answered: [
    ...makeView().answered,
    {
      question_id: 'q-2',
      position: 2,
      skill: 'TypeScript',
      question: 'What is a discriminated union?',
      answer: 'A tagged union.',
    },
  ],
});

class SessionApiStub {
  answer = vi.fn<
    (id: string, questionId: string, content: string, key: string) => Observable<SessionView>
  >(() => of(NEXT_VIEW));
  clarify = vi.fn<(id: string, text: string) => Observable<SessionMessage>>();
  get = vi.fn<(id: string) => Observable<SessionView>>(() => of(makeView()));
}

@Component({
  imports: [InterviewPanel],
  template: `<app-interview-panel [view]="view()" (viewChange)="onChange($event)" />`,
})
class Host {
  readonly view = signal<SessionView>(makeView());
  readonly emitted: SessionView[] = [];

  onChange(view: SessionView): void {
    this.emitted.push(view);
    this.view.set(view);
  }
}

describe('InterviewPanel', () => {
  let fixture: ComponentFixture<Host>;
  let host: Host;
  let root: HTMLElement;
  let api: SessionApiStub;
  let uuids: string[];

  const button = (label: string): HTMLButtonElement => {
    const found = Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };

  const textarea = (): HTMLTextAreaElement => {
    const found = root.querySelector<HTMLTextAreaElement>('textarea');
    if (!found) {
      throw new Error('Answer field not found');
    }
    return found;
  };

  const type = (value: string): void => {
    const field = textarea();
    field.value = value;
    field.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };

  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };

  beforeEach(async () => {
    api = new SessionApiStub();
    uuids = [];
    let n = 0;
    vi.spyOn(crypto, 'randomUUID').mockImplementation(() => {
      const id =
        `00000000-0000-4000-8000-00000000000${n++}` as `${string}-${string}-${string}-${string}-${string}`;
      uuids.push(id);
      return id;
    });
    await TestBed.configureTestingModule({
      imports: [Host],
      providers: [{ provide: SessionApi, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(Host);
    host = fixture.componentInstance;
    root = fixture.nativeElement as HTMLElement;
    await render();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the current question, its skill and the counter from the view in a live region', () => {
    expect(root.textContent).toContain('What is a discriminated union?');
    expect(root.textContent).toContain('TypeScript');
    const counter = root.querySelector('[aria-live="polite"]');
    expect(counter?.textContent?.replace(/\s+/g, ' ').trim()).toContain(
      'Answered 1 of 5 · 4 remaining',
    );
  });

  it('offers distinct submit and clarification actions', () => {
    expect(button('Submit answer')).toBeTruthy();
    expect(button('Ask for clarification')).toBeTruthy();
  });

  it('shows a character counter for the answer field', () => {
    type('hello');
    expect(root.textContent).toContain('5 / 5,000');
  });

  it('lists accepted answers read-only, without an edit button', () => {
    const answered = root.querySelector('[data-testid="answered-list"]');
    expect(answered?.textContent).toContain('What is a signal?');
    expect(answered?.textContent).toContain('A reactive value.');
    expect(answered?.querySelector('button, textarea, input, [contenteditable]')).toBeNull();
    const labels = Array.from(root.querySelectorAll('button')).map((b) =>
      (b.textContent ?? '').trim().toLowerCase(),
    );
    expect(labels.some((l) => l.includes('edit'))).toBe(false);
  });

  it('rejects an empty answer without calling the API', () => {
    type('   ');
    button('Submit answer').click();
    fixture.detectChanges();

    expect(api.answer).not.toHaveBeenCalled();
    expect(root.textContent).toContain('Please type an answer before submitting.');
  });

  it('rejects an answer over the limit without calling the API', () => {
    type('x'.repeat(5001));
    button('Submit answer').click();
    fixture.detectChanges();

    expect(api.answer).not.toHaveBeenCalled();
    expect(root.textContent).toContain('Answers are limited to 5,000 characters.');
  });

  it('accepts "I don\'t know" as an answer', () => {
    type("I don't know");
    button('Submit answer').click();

    expect(api.answer).toHaveBeenCalledWith('session-1', 'q-2', "I don't know", uuids[0]);
  });

  it('makes a single answer call on double click and disables the button while sending', () => {
    const pending = new Subject<SessionView>();
    api.answer.mockReturnValue(pending);
    type('A tagged union.');

    const submit = button('Submit answer');
    submit.click();
    submit.click();
    fixture.detectChanges();

    expect(api.answer).toHaveBeenCalledTimes(1);
    expect(button('Submit answer').disabled).toBe(true);
  });

  it('emits the new view, clears the field and moves focus to the next question', async () => {
    type('A tagged union.');
    button('Submit answer').click();
    await render();

    expect(host.emitted).toEqual([NEXT_VIEW]);
    expect(textarea().value).toBe('');
    expect(root.textContent).toContain('Answered 2 of 5 · 3 remaining');
    const focused = document.activeElement;
    expect(focused?.textContent).toContain('What is a Subject?');
  });

  it('reuses the same Idempotency-Key when retrying after a network error', async () => {
    const networkError: ApiError = {
      code: 'NETWORK_ERROR',
      message: 'Unable to reach the server.',
    };
    api.answer.mockReturnValueOnce(throwError(() => networkError));
    type('A tagged union.');

    button('Submit answer').click();
    await render();
    expect(button('Submit answer').disabled).toBe(false);

    button('Submit answer').click();
    await render();

    expect(api.answer).toHaveBeenCalledTimes(2);
    const firstKey = api.answer.mock.calls[0][3];
    const secondKey = api.answer.mock.calls[1][3];
    expect(firstKey).toBe(uuids[0]);
    expect(secondKey).toBe(firstKey);
  });

  it('uses a new Idempotency-Key when the answer changes after a failure', async () => {
    api.answer.mockReturnValueOnce(
      throwError(() => ({ code: 'NETWORK_ERROR', message: 'Unable to reach the server.' })),
    );
    type('A tagged union.');
    button('Submit answer').click();
    await render();

    type('A tagged union with a kind field.');
    button('Submit answer').click();
    await render();

    expect(api.answer.mock.calls[1][3]).not.toBe(api.answer.mock.calls[0][3]);
  });

  it('shows a toast and emits details.session on 409 QUESTION_ALREADY_ANSWERED', async () => {
    const conflict: ApiError = {
      code: 'QUESTION_ALREADY_ANSWERED',
      message: 'This question has already been answered. Showing the current question.',
      details: { session: NEXT_VIEW },
    };
    api.answer.mockReturnValue(throwError(() => conflict));
    type('Another answer.');

    button('Submit answer').click();
    await render();

    expect(host.emitted).toEqual([NEXT_VIEW]);
    const toast = root.querySelector('[data-testid="toast"]');
    expect(toast?.textContent).toContain(
      'This question has already been answered. Showing the current question.',
    );
    expect(root.textContent).toContain('What is a Subject?');
  });

  it('shows the toast and emits details.session on 409 NOT_CURRENT_QUESTION', async () => {
    api.answer.mockReturnValue(
      throwError(() => ({
        code: 'NOT_CURRENT_QUESTION',
        message: 'x',
        details: { session: NEXT_VIEW },
      })),
    );
    type('Stale answer.');

    button('Submit answer').click();
    await render();

    expect(host.emitted).toEqual([NEXT_VIEW]);
    expect(root.querySelector('[data-testid="toast"]')?.textContent).toContain(
      'This question has already been answered. Showing the current question.',
    );
  });

  it('shows the server message inline for ANSWER_TOO_LONG with the reported limit', async () => {
    api.answer.mockReturnValue(
      throwError(() => ({
        code: 'ANSWER_TOO_LONG',
        message: 'Answers are limited to 4,000 characters.',
        details: { limit: 4000 },
      })),
    );
    type('Some answer');

    button('Submit answer').click();
    await render();

    expect(root.textContent).toContain('Answers are limited to 4,000 characters.');
    expect(root.textContent).toContain('11 / 4,000');
    expect(host.emitted).toEqual([]);
  });

  it('on 503 CLARIFICATION_UNAVAILABLE shows the message and keeps the submit button enabled', async () => {
    api.clarify.mockReturnValue(
      throwError(() => ({ code: 'CLARIFICATION_UNAVAILABLE', message: 'LLM down' })),
    );
    type('What do you mean by union?');

    button('Ask for clarification').click();
    await render();

    expect(api.clarify).toHaveBeenCalledWith('session-1', 'What do you mean by union?');
    expect(root.textContent).toContain(
      'Clarifications are temporarily unavailable. You can still submit your answer.',
    );
    expect(button('Submit answer').disabled).toBe(false);
    expect(textarea().value).toBe('What do you mean by union?');
    expect(host.emitted).toEqual([]);
  });

  it('after a clarification reloads the view and emits it without counting an answer', async () => {
    const reply: SessionMessage = {
      id: 'm-1',
      role: 'assistant',
      kind: 'clarification_reply',
      content: 'A union with a shared tag.',
      created_at: '2026-01-01T00:01:00Z',
    };
    const refreshed = makeView({ messages: [reply] });
    api.clarify.mockReturnValue(of(reply));
    api.get.mockReturnValue(of(refreshed));
    type('What do you mean by union?');

    button('Ask for clarification').click();
    await render();

    expect(api.answer).not.toHaveBeenCalled();
    expect(api.get).toHaveBeenCalledWith('session-1');
    expect(host.emitted).toEqual([refreshed]);
    expect(textarea().value).toBe('');
  });

  it('does not render the answer form when there is no current question', async () => {
    host.view.set(makeView({ status: 'evaluating', current_question: null }));
    await render();

    expect(root.querySelector('textarea')).toBeNull();
    expect(root.textContent).toContain('What is a signal?');
  });
});
