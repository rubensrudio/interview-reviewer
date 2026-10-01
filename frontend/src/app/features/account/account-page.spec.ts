import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { routes } from '../../app.routes';
import { AuthApi, Me, MessageResponse } from '../../core/auth/auth-api';
import { authGuard, termsGuard } from '../../core/auth/auth-guards';
import { ApiError } from '../../core/http/api-error';
import { AccountPage } from './account-page';

@Component({ template: '' })
class BlankPage {}

const DELETE_CONFIRMATION =
  'This will permanently delete your account and all your data now. Backup copies expire within 30 days.';
const DELETED_MESSAGE = 'Your account and data were deleted. Backup copies expire within 30 days.';

const makeUser = (overrides: Partial<Me> = {}): Me => ({
  id: 'u-1',
  email: 'ana@example.com',
  has_password: true,
  google_linked: false,
  terms_accepted: true,
  ...overrides,
});

class AuthApiStub {
  readonly user = signal<Me | null>(makeUser());
  readonly currentUser = this.user.asReadonly();
  me = vi.fn<() => Observable<Me>>(() => of(makeUser()));
  deleteAccount = vi.fn<() => Observable<MessageResponse>>(() => {
    this.user.set(null);
    return of({ message: 'raw backend text' });
  });
}

describe('AccountPage', () => {
  let fixture: ComponentFixture<AccountPage>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const text = (): string => root.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map(
      (el) => el.textContent?.replace(/\s+/g, ' ').trim() ?? '',
    );
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };
  const deleteButton = (): HTMLButtonElement => buttonByText('Delete account');
  const dialog = (): HTMLElement | null => root.querySelector('dialog');
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const openAndConfirm = async (): Promise<void> => {
    deleteButton().click();
    await render();
    buttonByText('Delete account', dialog() as HTMLElement).click();
    await render();
  };

  const setup = async (configure?: (stub: AuthApiStub) => void): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'account', component: AccountPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    await router.navigateByUrl('/account');
    fixture = TestBed.createComponent(AccountPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.deleteAccount.mockReturnValue(throwError(() => err));

  it('is a top-level route guarded only by authGuard, outside the Shell (LAC-51)', () => {
    const account = routes.find((route) => route.path === 'account');

    expect(account).toBeDefined();
    expect(account?.canActivate).toEqual([authGuard]);
    expect(account?.canActivate).not.toContain(termsGuard);
    const shellChildren = routes.flatMap((route) => route.children ?? []);
    expect(shellChildren.some((route) => route.path === 'account')).toBe(false);
  });

  it('offers its own landmark and a way back, since it has no Shell', async () => {
    await setup();

    expect(root.querySelector('main')).not.toBeNull();
    expect(root.querySelector('a[href="/resumes"]')?.textContent?.trim()).toBe('Back to resumes');
  });

  it('shows the e-mail and the sign-in method of the current user', async () => {
    await setup();

    expect(root.querySelector('h1')?.textContent?.trim()).toBe('Account');
    expect(text()).toContain('ana@example.com');
    expect(text()).toContain('E-mail and password');
    expect(text()).not.toContain('Google');
  });

  it.each([
    [{ has_password: false, google_linked: true }, 'Google'],
    [{ has_password: true, google_linked: true }, 'E-mail and password, Google'],
  ])('describes the sign-in methods for %o', async (overrides, expected) => {
    await setup((stub) => stub.user.set(makeUser(overrides)));

    const method = root.querySelector('[data-testid="sign-in-method"]');
    expect(method?.textContent?.trim()).toBe(expected);
  });

  it('loads the user when it is not cached yet', async () => {
    await setup((stub) => {
      stub.user.set(null);
      stub.me.mockImplementation(() => {
        stub.user.set(makeUser({ email: 'bob@example.com' }));
        return of(makeUser({ email: 'bob@example.com' }));
      });
    });

    expect(api.me).toHaveBeenCalledTimes(1);
    expect(text()).toContain('bob@example.com');
  });

  it('shows an error with a retry when the user cannot be loaded', async () => {
    await setup((stub) => {
      stub.user.set(null);
      stub.me.mockReturnValue(
        throwError(() => ({ code: 'NETWORK_ERROR', message: 'raw backend text' })),
      );
    });

    expect(alerts()).toContain('Unable to reach the server.');
    expect(text()).not.toContain('raw backend text');

    api.me.mockImplementation(() => {
      api.user.set(makeUser());
      return of(makeUser());
    });
    buttonByText('Try again').click();
    await render();

    expect(text()).toContain('ana@example.com');
  });

  it('does not offer a temporary deactivation (LAC-12)', async () => {
    await setup();

    expect(text().toLowerCase()).not.toContain('deactivat');
  });

  it('opens the confirmation dialog with the section 9 text before deleting', async () => {
    await setup();

    deleteButton().click();
    await render();

    expect(dialog()).not.toBeNull();
    expect(dialog()?.textContent).toContain(DELETE_CONFIRMATION);
    expect(api.deleteAccount).not.toHaveBeenCalled();
  });

  it('does not delete when the dialog is cancelled', async () => {
    await setup();

    deleteButton().click();
    await render();
    buttonByText('Cancel', dialog() as HTMLElement).click();
    await render();

    expect(api.deleteAccount).not.toHaveBeenCalled();
    expect(dialog()).toBeNull();
    expect(router.url).toBe('/account');
  });

  it('calls deleteAccount only after confirming, then clears the user and keeps the confirmation on screen', async () => {
    await setup();

    await openAndConfirm();

    expect(api.deleteAccount).toHaveBeenCalledTimes(1);
    expect(api.currentUser()).toBeNull();
    const status = root.querySelector<HTMLElement>('[role="status"]');
    expect(status?.textContent).toContain(DELETED_MESSAGE);
    expect(document.activeElement).toBe(status);
    expect(text()).not.toContain('raw backend text');
    expect(router.url).toBe('/account');
    expect(root.querySelector('a[href="/resumes"]')).toBeNull();
    expect(root.querySelector('button')).toBeNull();
  });

  it('goes to /login from the confirmation, replacing the account page in history', async () => {
    await setup();
    const navigate = vi.spyOn(router, 'navigateByUrl');

    await openAndConfirm();
    const link = root.querySelector<HTMLAnchorElement>('a[href="/login"]');
    expect(link?.textContent?.trim()).toBe('Go to sign in');
    link?.click();
    await render();

    expect(router.url).toBe('/login');
    expect(navigate).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ replaceUrl: true }),
    );
  });

  it('blocks a second request while the deletion is in flight', async () => {
    const pending = new Subject<MessageResponse>();
    await setup((stub) => stub.deleteAccount.mockReturnValue(pending.asObservable()));

    await openAndConfirm();

    expect(dialog()).toBeNull();
    expect(deleteButton().disabled).toBe(true);
    expect(deleteButton().getAttribute('aria-busy')).toBe('true');
    deleteButton().click();
    await render();
    expect(dialog()).toBeNull();
    expect(api.deleteAccount).toHaveBeenCalledTimes(1);
  });

  it('401 AUTH_REQUIRED sends the user to /login', async () => {
    await setup(failWith({ code: 'AUTH_REQUIRED', message: 'raw backend text' }));

    await openAndConfirm();

    expect(router.url).toBe('/login');
  });

  it.each([
    ['CSRF_FAILED', 'Your session expired. Please reload the page.'],
    ['NETWORK_ERROR', 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text and stays on the page', async (code, expected) => {
    await setup(failWith({ code, message: 'raw backend text' }));

    await openAndConfirm();

    expect(alerts()).toContain(expected);
    expect(text()).not.toContain('raw backend text');
    expect(router.url).toBe('/account');
    expect(deleteButton().disabled).toBe(false);
  });
});
