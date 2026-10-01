import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { SessionApi, SessionSummary } from '../sessions/session-api';
import { HistoryPage } from './history-page';

function makeSession(overrides: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id: 's-1',
    created_at: '2026-03-10T12:00:00Z',
    status: 'completed',
    resume_name: 'backend.pdf',
    required_skills: ['Python', 'SQL'],
    completed_at: '2026-03-10T13:00:00Z',
    ...overrides,
  };
}

class SessionApiStub {
  list = vi.fn<() => Observable<SessionSummary[]>>(() => of([]));
  delete = vi.fn<(id: string) => Observable<void>>(() => of(undefined));
}

describe('HistoryPage', () => {
  let fixture: ComponentFixture<HistoryPage>;
  let api: SessionApiStub;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const rows = (): HTMLElement[] =>
    Array.from(root.querySelectorAll<HTMLElement>('[data-testid="session-item"]'));
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const checkbox = (row: HTMLElement): HTMLInputElement | null =>
    row.querySelector<HTMLInputElement>('input[type="checkbox"]');

  async function settle(): Promise<void> {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(HistoryPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    await settle();
  }

  async function toggle(row: HTMLElement): Promise<void> {
    const box = checkbox(row);
    if (!box) {
      throw new Error('checkbox not found');
    }
    box.click();
    await settle();
  }

  beforeEach(async () => {
    api = new SessionApiStub();
    await TestBed.configureTestingModule({
      imports: [HistoryPage],
      providers: [provideRouter([]), { provide: SessionApi, useValue: api }],
    }).compileComponents();
  });

  afterEach(() => {
    fixture?.destroy();
    root?.remove();
  });

  it('shows the empty state with a link to start a session (DATA-92)', async () => {
    await render();

    expect(text()).toContain('No interviews yet. Start your first one.');
    const link = root.querySelector<HTMLAnchorElement>('a[href="/sessions/new"]');
    expect(link).not.toBeNull();
    expect(rows()).toHaveLength(0);
  });

  it('lists date, resume name, required skills and status, including cancelled and expired (DATA-01)', async () => {
    api.list.mockReturnValue(
      of([
        makeSession({ id: 'a' }),
        makeSession({ id: 'b', status: 'cancelled', completed_at: null }),
        makeSession({ id: 'c', status: 'expired', completed_at: null }),
      ]),
    );
    await render();

    expect(rows()).toHaveLength(3);
    const first = rows()[0];
    expect(first.textContent).toContain('backend.pdf');
    expect(first.textContent).toContain('Python');
    expect(first.textContent).toContain('SQL');
    expect(first.textContent).toContain('Completed');
    expect(first.querySelector('time')?.getAttribute('datetime')).toBe('2026-03-10T12:00:00Z');
    expect(rows()[1].textContent).toContain('Cancelled');
    expect(rows()[2].textContent).toContain('Expired');
  });

  it('shows "Deleted resume" when resume_name is null', async () => {
    api.list.mockReturnValue(of([makeSession({ resume_name: null })]));
    await render();

    expect(rows()[0].textContent).toContain('Deleted resume');
  });

  it('links to the report only for completed sessions, never inline', async () => {
    api.list.mockReturnValue(
      of([makeSession({ id: 'a' }), makeSession({ id: 'b', status: 'cancelled' })]),
    );
    await render();

    expect(rows()[0].querySelector('a[href="/sessions/a/report"]')).not.toBeNull();
    expect(rows()[1].querySelector('a[href="/sessions/b/report"]')).toBeNull();
  });

  it('deletes only after confirmation and removes the row (DATA-05)', async () => {
    api.list.mockReturnValue(of([makeSession({ id: 'a' }), makeSession({ id: 'b' })]));
    await render();

    buttonByText('Delete', rows()[0]).click();
    await settle();
    expect(api.delete).not.toHaveBeenCalled();
    const dialog = root.querySelector('app-confirm-dialog');
    expect(dialog).not.toBeNull();

    buttonByText('Delete', dialog as HTMLElement).click();
    await settle();

    expect(api.delete).toHaveBeenCalledWith('a');
    expect(rows()).toHaveLength(1);
    expect(root.querySelector('app-confirm-dialog')).toBeNull();
  });

  it('does not delete when the confirmation is cancelled', async () => {
    api.list.mockReturnValue(of([makeSession({ id: 'a' })]));
    await render();

    buttonByText('Delete', rows()[0]).click();
    await settle();
    buttonByText('Cancel', root.querySelector('app-confirm-dialog') as HTMLElement).click();
    await settle();

    expect(api.delete).not.toHaveBeenCalled();
    expect(rows()).toHaveLength(1);
  });

  it('keeps the row and shows an error when delete fails', async () => {
    api.list.mockReturnValue(of([makeSession({ id: 'a' })]));
    api.delete.mockReturnValue(
      throwError(() => ({ code: 'RESOURCE_NOT_FOUND', message: 'Not found.' }) satisfies ApiError),
    );
    await render();

    buttonByText('Delete', rows()[0]).click();
    await settle();
    buttonByText('Delete', root.querySelector('app-confirm-dialog') as HTMLElement).click();
    await settle();

    expect(rows()).toHaveLength(1);
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('Not found.');
  });

  it('enables "Compare" only with exactly two completed sessions selected (CMP-01)', async () => {
    api.list.mockReturnValue(
      of([
        makeSession({ id: 'a' }),
        makeSession({ id: 'b' }),
        makeSession({ id: 'c' }),
        makeSession({ id: 'd', status: 'cancelled', completed_at: null }),
      ]),
    );
    await render();
    const compare = (): HTMLButtonElement => buttonByText('Compare');

    expect(checkbox(rows()[3])).toBeNull();
    expect(compare().disabled).toBe(true);

    await toggle(rows()[0]);
    expect(compare().disabled).toBe(true);

    await toggle(rows()[1]);
    expect(compare().disabled).toBe(false);

    await toggle(rows()[2]);
    expect(compare().disabled).toBe(true);

    await toggle(rows()[2]);
    expect(compare().disabled).toBe(false);
  });

  it('navigates to the comparison with both ids', async () => {
    api.list.mockReturnValue(of([makeSession({ id: 'a' }), makeSession({ id: 'b' })]));
    await render();
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    await toggle(rows()[0]);
    await toggle(rows()[1]);
    buttonByText('Compare').click();

    expect(navigate).toHaveBeenCalledWith(['/compare'], { queryParams: { a: 'a', b: 'b' } });
  });

  it('shows an error with a retry when the list fails to load', async () => {
    api.list.mockReturnValueOnce(
      throwError(() => ({ code: 'NETWORK_ERROR', message: '' }) satisfies ApiError),
    );
    await render();

    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Unable to reach the server.',
    );
    api.list.mockReturnValue(of([makeSession()]));
    buttonByText('Try again').click();
    await settle();

    expect(rows()).toHaveLength(1);
  });
});
