import {
  HttpClient,
  HttpXsrfTokenExtractor,
  provideHttpClient,
  withInterceptors,
} from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { authInterceptor } from './auth-interceptor';

describe('authInterceptor', () => {
  let http: HttpClient;
  let httpTesting: HttpTestingController;
  let navigateByUrl: ReturnType<typeof vi.fn>;
  let currentUrl: string;

  beforeEach(() => {
    currentUrl = '/resumes?page=2';
    navigateByUrl = vi.fn().mockResolvedValue(true);
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([authInterceptor])),
        provideHttpClientTesting(),
        { provide: HttpXsrfTokenExtractor, useValue: { getToken: () => 'xsrf-123' } },
        {
          provide: Router,
          useValue: {
            navigateByUrl,
            get url() {
              return currentUrl;
            },
          },
        },
      ],
    });
    http = TestBed.inject(HttpClient);
    httpTesting = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpTesting.verify());

  function failWith(url: string, status: number, code: string): unknown {
    let received: unknown;
    http.get(url).subscribe({ error: (e: unknown) => (received = e) });
    httpTesting
      .expectOne(url)
      .flush({ error: { code, message: code, details: null } }, { status, statusText: 'Error' });
    return received;
  }

  it('navigates to /login with returnUrl on 401 from /api/resumes', () => {
    const received = failWith('/api/resumes', 401, 'AUTH_REQUIRED');

    expect(navigateByUrl).toHaveBeenCalledWith('/login?returnUrl=%2Fresumes%3Fpage%3D2');
    expect(received).toBeTruthy();
  });

  it('navigates to /accept-terms on 403 TERMS_REQUIRED', () => {
    failWith('/api/resumes', 403, 'TERMS_REQUIRED');

    expect(navigateByUrl).toHaveBeenCalledWith('/accept-terms');
  });

  it('does not navigate on 403 CSRF_FAILED', () => {
    failWith('/api/resumes', 403, 'CSRF_FAILED');

    expect(navigateByUrl).not.toHaveBeenCalled();
  });

  it('does not navigate on 401 INVALID_CREDENTIALS from login', () => {
    let received: unknown;
    http.post('/api/auth/login', {}).subscribe({ error: (e: unknown) => (received = e) });
    httpTesting
      .expectOne('/api/auth/login')
      .flush(
        { error: { code: 'INVALID_CREDENTIALS', message: 'x', details: null } },
        { status: 401, statusText: 'Unauthorized' },
      );

    expect(navigateByUrl).not.toHaveBeenCalled();
    expect(received).toBeTruthy();
  });

  it('leaves 401 from /api/auth/me to the route guards', () => {
    failWith('/api/auth/me', 401, 'AUTH_REQUIRED');

    expect(navigateByUrl).not.toHaveBeenCalled();
  });

  it('does not redirect again when already on the login page', () => {
    currentUrl = '/login?returnUrl=%2Fresumes';
    failWith('/api/resumes', 401, 'AUTH_REQUIRED');

    expect(navigateByUrl).not.toHaveBeenCalled();
  });

  it('does not redirect again when already on /accept-terms', () => {
    currentUrl = '/accept-terms';
    failWith('/api/resumes', 403, 'TERMS_REQUIRED');

    expect(navigateByUrl).not.toHaveBeenCalled();
  });

  it('passes successful responses through untouched', () => {
    let body: unknown;
    http.get('/api/resumes').subscribe((b) => (body = b));
    httpTesting.expectOne('/api/resumes').flush({ items: [] });

    expect(body).toEqual({ items: [] });
    expect(navigateByUrl).not.toHaveBeenCalled();
  });

  it('adds X-XSRF-TOKEN to HEAD requests, which the built-in XSRF interceptor skips', () => {
    http.head('/api/resumes').subscribe();
    const req = httpTesting.expectOne('/api/resumes');

    expect(req.request.headers.get('X-XSRF-TOKEN')).toBe('xsrf-123');
    req.flush(null);
  });

  it('does not add X-XSRF-TOKEN to GET requests', () => {
    http.get('/api/resumes').subscribe();
    const req = httpTesting.expectOne('/api/resumes');

    expect(req.request.headers.has('X-XSRF-TOKEN')).toBe(false);
    req.flush({});
  });
});
