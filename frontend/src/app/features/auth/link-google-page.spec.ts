import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi, Me } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { LinkGooglePage } from './link-google-page';

@Component({ template: '' })
class BlankPage {}

const TOKEN = 'link-token-123';
const INTRO =
  'An account with this e-mail already exists. Enter its password to link your Google account.';
const INVALID_CREDENTIALS_TEXT = 'Invalid e-mail or password.';
const LINK_INVALID_TEXT = 'This link is invalid or has expired. Request a new one.';
const USER: Me = {
  id: 'u-1',
  email: 'ana@example.com',
  has_password: true,
  google_linked: true,
  terms_accepted: true,
};

class AuthApiStub {
  linkGoogle = vi.fn<(token: string, password: string) => Observable<Me>>(() => of(USER));
}

describe('LinkGooglePage', () => {
  let fixture: ComponentFixture<LinkGooglePage>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map(
      (el) => el.textContent?.replace(/\s+/g, ' ').trim() ?? '',
    );
  const passwordInput = (): HTMLInputElement | null =>
    root.querySelector<HTMLInputElement>('#link-password');
  const requirePasswordInput = (): HTMLInputElement => {
    const el = passwordInput();
    if (!el) {
      throw new Error('Password input not found');
    }
    return el;
  };
  const submitButton = (): HTMLButtonElement => {
    const found = root.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (!found) {
      throw new Error('Submit button not found');
    }
    return found;
  };
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const typePassword = async (value: string): Promise<void> => {
    const el = requirePasswordInput();
    el.value = value;
    el.dispatchEvent(new Event('input'));
    await render();
  };
  const submit = async (): Promise<void> => {
    submitButton().click();
    await render();
  };

  const setup = async (
    url = `/link-google?token=${TOKEN}`,
    configure?: (stub: AuthApiStub) => void,
  ): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'link-google', component: LinkGooglePage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    await router.navigateByUrl(url);
    fixture = TestBed.createComponent(LinkGooglePage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.linkGoogle.mockReturnValue(throwError(() => err));

  it('explains the link, renders a labelled password field and never renders the token', async () => {
    await setup();

    expect(root.textContent?.replace(/\s+/g, ' ')).toContain(INTRO);
    const input = requirePasswordInput();
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('current-password');
    expect(input.getAttribute('maxlength')).toBe('1024');
    expect(input.required).toBe(true);
    expect(root.querySelector('label[for="link-password"]')?.textContent?.trim()).toBeTruthy();
    expect(root.querySelector('h1')?.textContent?.trim()).toBeTruthy();
    expect(root.innerHTML).not.toContain(TOKEN);
  });

  it('200 sends the token and the password, then navigates to /resumes', async () => {
    await setup();

    await typePassword('my-local-password');
    await submit();

    expect(api.linkGoogle).toHaveBeenCalledTimes(1);
    expect(api.linkGoogle).toHaveBeenCalledWith(TOKEN, 'my-local-password');
    expect(router.url).toBe('/resumes');
  });

  it('401 shows "Invalid e-mail or password." and does not navigate', async () => {
    await setup(undefined, failWith({ code: 'INVALID_CREDENTIALS', message: 'raw backend text' }));

    await typePassword('wrong-password');
    await submit();

    expect(alerts()).toContain(INVALID_CREDENTIALS_TEXT);
    expect(root.textContent).not.toContain('raw backend text');
    expect(router.url).toBe(`/link-google?token=${TOKEN}`);
    expect(passwordInput()).not.toBeNull();
    expect(submitButton().disabled).toBe(false);
  });

  it('lets the user retry after a wrong password', async () => {
    await setup(undefined, failWith({ code: 'INVALID_CREDENTIALS', message: '' }));

    await typePassword('wrong-password');
    await submit();
    api.linkGoogle.mockReturnValue(of(USER));
    await typePassword('right-password');
    await submit();

    expect(api.linkGoogle).toHaveBeenLastCalledWith(TOKEN, 'right-password');
    expect(router.url).toBe('/resumes');
  });

  it('400 LINK_INVALID shows the catalog text with a link back to sign in', async () => {
    await setup(undefined, failWith({ code: 'LINK_INVALID', message: 'raw backend text' }));

    await typePassword('my-local-password');
    await submit();

    expect(alerts()).toContain(LINK_INVALID_TEXT);
    expect(root.querySelector('a[href="/login"]')).not.toBeNull();
    expect(root.textContent).not.toContain('raw backend text');
    expect(passwordInput()).toBeNull();
    expect(router.url).not.toBe('/resumes');
  });

  it('a missing token shows the invalid-link state without calling the API', async () => {
    await setup('/link-google');

    expect(api.linkGoogle).not.toHaveBeenCalled();
    expect(alerts()).toContain(LINK_INVALID_TEXT);
    expect(root.querySelector('a[href="/login"]')).not.toBeNull();
    expect(passwordInput()).toBeNull();
  });

  it('flags an empty password without calling the API', async () => {
    await setup();

    await submit();

    expect(api.linkGoogle).not.toHaveBeenCalled();
    const input = requirePasswordInput();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('link-password-error');
    expect(root.querySelector('#link-password-error')?.getAttribute('role')).toBe('alert');
    expect(document.activeElement).toBe(input);
  });

  it('flags an over-long password without calling the API', async () => {
    await setup();

    await typePassword('a'.repeat(1025));
    await submit();

    expect(api.linkGoogle).not.toHaveBeenCalled();
    expect(root.querySelector('#link-password-error')?.textContent).toContain('1024');
  });

  it('does not judge the password on the client', async () => {
    await setup();

    await typePassword('1');
    await submit();

    expect(api.linkGoogle).toHaveBeenCalledWith(TOKEN, '1');
  });

  it('disables the button while the request is in flight', async () => {
    const pending = new Subject<Me>();
    await setup(undefined, (stub) => stub.linkGoogle.mockReturnValue(pending.asObservable()));

    await typePassword('my-local-password');
    await submit();

    expect(submitButton().disabled).toBe(true);
    expect(submitButton().getAttribute('aria-busy')).toBe('true');
    submitButton().click();
    await render();
    expect(api.linkGoogle).toHaveBeenCalledTimes(1);

    pending.error({ code: 'NETWORK_ERROR', message: '' } satisfies ApiError);
    await render();

    expect(submitButton().disabled).toBe(false);
    expect(alerts()).toContain('Unable to reach the server.');
  });

  it.each([
    ['TOO_MANY_ATTEMPTS', 'Too many attempts. Please try again later.'],
    ['VALIDATION_ERROR', 'Please check the highlighted fields.'],
    ['CSRF_FAILED', 'Your session expired. Please reload the page.'],
    ['NETWORK_ERROR', 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text and keeps the form', async (code, expected) => {
    await setup(undefined, failWith({ code, message: 'raw backend text' }));

    await typePassword('my-local-password');
    await submit();

    expect(alerts()).toContain(expected);
    expect(root.textContent).not.toContain('raw backend text');
    expect(passwordInput()).not.toBeNull();
    expect(submitButton().disabled).toBe(false);
  });
});
