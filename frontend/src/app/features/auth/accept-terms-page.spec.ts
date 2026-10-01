import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { AuthApi } from '../../core/auth/auth-api';
import { ApiError } from '../../core/http/api-error';
import { AcceptTermsPage } from './accept-terms-page';

@Component({ template: '' })
class BlankPage {}

class AuthApiStub {
  acceptTerms = vi.fn<() => Observable<void>>(() => of(undefined));
  logout = vi.fn<() => Observable<void>>(() => of(undefined));
}

describe('AcceptTermsPage', () => {
  let fixture: ComponentFixture<AcceptTermsPage>;
  let root: HTMLElement;
  let api: AuthApiStub;
  let router: Router;

  const alerts = (): string[] =>
    Array.from(root.querySelectorAll('[role="alert"]')).map(
      (el) => el.textContent?.replace(/\s+/g, ' ').trim() ?? '',
    );
  const checkbox = (): HTMLInputElement => {
    const el = root.querySelector<HTMLInputElement>('#accept-terms');
    if (!el) {
      throw new Error('Checkbox not found');
    }
    return el;
  };
  const submitButton = (): HTMLButtonElement => {
    const el = root.querySelector<HTMLButtonElement>('button[type="submit"]');
    if (!el) {
      throw new Error('Submit button not found');
    }
    return el;
  };
  const signOutButton = (): HTMLButtonElement => {
    const el = root.querySelector<HTMLButtonElement>('button.secondary');
    if (!el) {
      throw new Error('Sign out button not found');
    }
    return el;
  };
  const render = async (): Promise<void> => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const tick = async (): Promise<void> => {
    checkbox().click();
    await render();
  };
  const submit = async (): Promise<void> => {
    submitButton().click();
    await render();
  };

  const setup = async (
    url = '/accept-terms',
    configure?: (stub: AuthApiStub) => void,
  ): Promise<void> => {
    api = new AuthApiStub();
    configure?.(api);
    await TestBed.configureTestingModule({
      providers: [
        provideRouter([
          { path: 'accept-terms', component: AcceptTermsPage },
          { path: '**', component: BlankPage },
        ]),
        { provide: AuthApi, useValue: api },
      ],
    }).compileComponents();
    router = TestBed.inject(Router);
    await router.navigateByUrl(url);
    fixture = TestBed.createComponent(AcceptTermsPage);
    root = fixture.nativeElement as HTMLElement;
    await render();
  };

  const failWith = (err: ApiError) => (stub: AuthApiStub) =>
    stub.acceptTerms.mockReturnValue(throwError(() => err));

  it('renders a heading, a labelled checkbox and links to the terms and the privacy policy', async () => {
    await setup();

    expect(root.querySelector('h1')?.textContent?.trim()).toBeTruthy();
    expect(checkbox().type).toBe('checkbox');
    expect(checkbox().checked).toBe(false);
    const label = root.querySelector('label[for="accept-terms"]');
    expect(label?.textContent?.replace(/\s+/g, ' ')).toContain(
      'I accept the Terms of Use and the Privacy Policy.',
    );
    expect(root.querySelector('a[href="/terms"]')).not.toBeNull();
    expect(root.querySelector('a[href="/privacy"]')).not.toBeNull();
  });

  it('keeps the button disabled until the checkbox is ticked', async () => {
    await setup();

    expect(submitButton().disabled).toBe(true);
    submitButton().click();
    await render();
    expect(api.acceptTerms).not.toHaveBeenCalled();

    await tick();
    expect(submitButton().disabled).toBe(false);

    await tick();
    expect(submitButton().disabled).toBe(true);
  });

  it('confirming calls acceptTerms and navigates to /resumes', async () => {
    await setup();

    await tick();
    await submit();

    expect(api.acceptTerms).toHaveBeenCalledTimes(1);
    expect(router.url).toBe('/resumes');
  });

  it('honours an internal returnUrl after accepting', async () => {
    await setup('/accept-terms?returnUrl=%2Fhistory');

    await tick();
    await submit();

    expect(router.url).toBe('/history');
  });

  it('ignores an external returnUrl', async () => {
    await setup('/accept-terms?returnUrl=https%3A%2F%2Fevil.example');

    await tick();
    await submit();

    expect(router.url).toBe('/resumes');
  });

  it('disables the button while the request is in flight', async () => {
    const pending = new Subject<void>();
    await setup(undefined, (stub) => stub.acceptTerms.mockReturnValue(pending.asObservable()));

    await tick();
    await submit();

    expect(submitButton().disabled).toBe(true);
    expect(submitButton().getAttribute('aria-busy')).toBe('true');
    submitButton().click();
    await render();
    expect(api.acceptTerms).toHaveBeenCalledTimes(1);

    pending.error({ code: 'NETWORK_ERROR', message: '' } satisfies ApiError);
    await render();

    expect(submitButton().disabled).toBe(false);
    expect(alerts()).toContain('Unable to reach the server.');
  });

  it('401 AUTH_REQUIRED sends the user to /login', async () => {
    await setup(undefined, failWith({ code: 'AUTH_REQUIRED', message: 'raw backend text' }));

    await tick();
    await submit();

    expect(router.url).toBe('/login');
  });

  it.each([
    ['VALIDATION_ERROR', 'Please check the highlighted fields.'],
    ['CSRF_FAILED', 'Your session expired. Please reload the page.'],
    ['NETWORK_ERROR', 'Unable to reach the server.'],
    ['SOMETHING_ELSE', 'Something went wrong. Please try again.'],
  ])('maps %s to the catalog text and stays on the page', async (code, expected) => {
    await setup(undefined, failWith({ code, message: 'raw backend text' }));

    await tick();
    await submit();

    expect(alerts()).toContain(expected);
    expect(root.textContent).not.toContain('raw backend text');
    expect(router.url).toBe('/accept-terms');
    expect(submitButton().disabled).toBe(false);
  });

  it('lets the user sign out instead of accepting', async () => {
    await setup();

    signOutButton().click();
    await render();

    expect(api.logout).toHaveBeenCalledTimes(1);
    expect(api.acceptTerms).not.toHaveBeenCalled();
    expect(router.url).toBe('/login');
  });

  it('treats a 401 on sign out as already signed out', async () => {
    await setup(undefined, (stub) =>
      stub.logout.mockReturnValue(throwError(() => ({ code: 'AUTH_REQUIRED', message: '' }))),
    );

    signOutButton().click();
    await render();

    expect(router.url).toBe('/login');
  });

  it('shows an error when sign out fails', async () => {
    await setup(undefined, (stub) =>
      stub.logout.mockReturnValue(throwError(() => ({ code: 'NETWORK_ERROR', message: '' }))),
    );

    signOutButton().click();
    await render();

    expect(alerts()).toContain('Could not sign out. Please try again.');
    expect(router.url).toBe('/accept-terms');
  });
});
