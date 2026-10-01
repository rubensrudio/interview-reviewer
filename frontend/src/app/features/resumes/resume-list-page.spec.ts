import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Observable, Subject, of, throwError } from 'rxjs';

import { ApiError } from '../../core/http/api-error';
import { ResumeApi, ResumeStatus, ResumeSummary } from './resume-api';
import { ResumeListPage } from './resume-list-page';

const DELETE_CONFIRMATION =
  'This will permanently delete the file, its text and extraction. Sessions that used it will keep only the skills and cited evidence until you delete them.';
const NO_TEXT_MESSAGE =
  "We couldn't read text from this PDF. Scanned documents are not supported yet — please upload a text-based PDF.";

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

class ResumeApiStub {
  list = vi.fn<(status?: ResumeStatus) => Observable<ResumeSummary[]>>(() => of([]));
  upload = vi.fn<(file: File) => Observable<ResumeSummary>>();
  delete = vi.fn<(id: string) => Observable<void>>(() => of(undefined));
}

function pdf(size: number, name = 'resume.pdf'): File {
  const file = new File(['%PDF'], name, { type: 'application/pdf' });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

describe('ResumeListPage', () => {
  let fixture: ComponentFixture<ResumeListPage>;
  let api: ResumeApiStub;
  let root: HTMLElement;

  const text = (): string => root.textContent ?? '';
  const rows = (): HTMLElement[] =>
    Array.from(root.querySelectorAll<HTMLElement>('[data-testid="resume-item"]'));
  const buttonByText = (label: string, scope: ParentNode = root): HTMLButtonElement => {
    const found = Array.from(scope.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent?.trim() === label,
    );
    if (!found) {
      throw new Error(`Button "${label}" not found`);
    }
    return found;
  };

  async function render(): Promise<void> {
    fixture = TestBed.createComponent(ResumeListPage);
    root = fixture.nativeElement as HTMLElement;
    document.body.appendChild(root);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function choose(file: File): Promise<void> {
    const input = root.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) {
      throw new Error('file input not found');
    }
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    input.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  beforeEach(async () => {
    api = new ResumeApiStub();
    await TestBed.configureTestingModule({
      imports: [ResumeListPage],
      providers: [provideRouter([]), { provide: ResumeApi, useValue: api }],
    }).compileComponents();
  });

  afterEach(() => {
    vi.useRealTimers();
    fixture?.destroy();
    root?.remove();
  });

  it('lists own versions with name, upload date and status (CV-13)', async () => {
    api.list.mockReturnValue(
      of([
        makeResume({ id: 'a', filename: 'backend.pdf', status: 'ready' }),
        makeResume({ id: 'b', filename: 'frontend.pdf', status: 'received' }),
      ]),
    );
    await render();

    expect(rows()).toHaveLength(2);
    expect(rows()[0].textContent).toContain('backend.pdf');
    expect(rows()[0].textContent).toContain('Ready');
    expect(rows()[1].textContent).toContain('Received');
    const time = rows()[0].querySelector('time');
    expect(time?.getAttribute('datetime')).toBe('2026-03-10T12:00:00Z');
    expect(time?.textContent?.trim()).not.toBe('');
  });

  it('links each ready version to its detail page (LAC-48)', async () => {
    api.list.mockReturnValue(
      of([
        makeResume({ id: 'a', filename: 'backend.pdf', status: 'ready' }),
        makeResume({ id: 'b', filename: 'frontend.pdf', status: 'processing' }),
        makeResume({ id: 'c', filename: 'broken.pdf', status: 'failed' }),
      ]),
    );
    await render();

    const link = rows()[0].querySelector('a');
    expect(link?.getAttribute('href')).toBe('/resumes/a');
    expect(link?.textContent?.trim()).toBe('backend.pdf');
    expect(rows()[1].querySelector('a')).toBeNull();
    expect(rows()[2].querySelector('a')).toBeNull();
  });

  it('shows the empty state when there are no versions', async () => {
    await render();
    expect(rows()).toHaveLength(0);
    expect(text()).toContain('No resumes yet.');
  });

  it('shows a retryable error when the list fails', async () => {
    api.list.mockReturnValueOnce(
      throwError(() => ({ code: 'NETWORK_ERROR', message: 'x' }) satisfies ApiError),
    );
    await render();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Unable to reach the server.',
    );
    api.list.mockReturnValue(of([makeResume()]));
    buttonByText('Try again').click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(rows()).toHaveLength(1);
  });

  it('announces the status of each item in a polite live region', async () => {
    api.list.mockReturnValue(
      of([makeResume({ id: 'a', status: 'processing' }), makeResume({ id: 'b', status: 'ready' })]),
    );
    await render();

    for (const row of rows()) {
      const live = row.querySelector('[aria-live="polite"]');
      expect(live).not.toBeNull();
    }
    expect(rows()[0].querySelector('[aria-live="polite"]')?.textContent).toContain('Processing');
    expect(rows()[1].querySelector('[aria-live="polite"]')?.textContent).toContain('Ready');
  });

  it('updates a processing item that becomes ready on the next poll, without a new upload (CV-05)', async () => {
    vi.useFakeTimers();
    api.list
      .mockReturnValueOnce(of([makeResume({ status: 'processing' })]))
      .mockReturnValue(of([makeResume({ status: 'ready' })]));
    await render();
    expect(rows()[0].textContent).toContain('Processing');

    await vi.advanceTimersByTimeAsync(3000);
    fixture.detectChanges();

    expect(api.list).toHaveBeenCalledTimes(2);
    expect(api.upload).not.toHaveBeenCalled();
    expect(rows()[0].textContent).toContain('Ready');

    await vi.advanceTimersByTimeAsync(9000);
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  it('does not poll when every version is finished', async () => {
    vi.useFakeTimers();
    api.list.mockReturnValue(of([makeResume({ status: 'failed', failure_code: 'NO_TEXT' })]));
    await render();
    await vi.advanceTimersByTimeAsync(9000);
    expect(api.list).toHaveBeenCalledTimes(1);
  });

  it('maps failure_code NO_TEXT to the section 9 message, never the raw failure_message (CV-06)', async () => {
    api.list.mockReturnValue(
      of([makeResume({ status: 'failed', failure_code: 'NO_TEXT', failure_message: 'raw' })]),
    );
    await render();
    expect(rows()[0].textContent).toContain(NO_TEXT_MESSAGE);
    expect(rows()[0].textContent).not.toContain('raw');
  });

  it('maps NOT_ENGLISH and unavailable-LLM failures to their section 9 messages (CV-14, CV-93)', async () => {
    api.list.mockReturnValue(
      of([
        makeResume({ id: 'a', status: 'failed', failure_code: 'NOT_ENGLISH' }),
        makeResume({ id: 'b', status: 'failed', failure_code: 'LLM_UNAVAILABLE' }),
        makeResume({ id: 'c', status: 'failed', failure_code: 'SOMETHING_NEW' }),
      ]),
    );
    await render();
    expect(rows()[0].textContent).toContain('Only resumes in English are supported at the moment.');
    const retryLater = "We couldn't process this resume. Please try uploading it again later.";
    expect(rows()[1].textContent).toContain(retryLater);
    expect(rows()[2].textContent).toContain(retryLater);
  });

  it('uploads the chosen PDF and adds the received version to the list (CV-01)', async () => {
    const created = makeResume({ id: 'new', filename: 'resume.pdf', status: 'received' });
    api.upload.mockReturnValue(of(created));
    await render();

    const file = pdf(1024);
    await choose(file);

    expect(api.upload).toHaveBeenCalledWith(file);
    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain('resume.pdf');
    expect(rows()[0].textContent).toContain('Received');
  });

  it('shows the size limit from the 413 details (CV-03)', async () => {
    api.upload.mockReturnValue(
      throwError(
        () =>
          ({
            code: 'FILE_TOO_LARGE',
            message: 'raw',
            details: { limit_bytes: 5242880 },
          }) satisfies ApiError,
      ),
    );
    await render();
    await choose(pdf(1024));

    expect(root.querySelector('[role="alert"]')?.textContent?.trim()).toBe(
      'The file exceeds the 5 MB limit.',
    );
  });

  it('shows the resume limit message on 409 RESUME_LIMIT_REACHED (CV-04)', async () => {
    api.upload.mockReturnValue(
      throwError(
        () =>
          ({
            code: 'RESUME_LIMIT_REACHED',
            message: 'raw',
            details: { limit: 10 },
          }) satisfies ApiError,
      ),
    );
    await render();
    await choose(pdf(1024));

    expect(root.querySelector('[role="alert"]')?.textContent?.trim()).toBe(
      'You have reached the limit of 10 resumes. Delete one to upload another.',
    );
  });

  it('uses the limits sent by the backend instead of fixed numbers', async () => {
    api.upload
      .mockReturnValueOnce(
        throwError(
          () =>
            ({
              code: 'FILE_TOO_LARGE',
              message: 'raw',
              details: { limit_bytes: 8 * 1024 * 1024 },
            }) satisfies ApiError,
        ),
      )
      .mockReturnValueOnce(
        throwError(
          () =>
            ({
              code: 'RESUME_LIMIT_REACHED',
              message: 'raw',
              details: { limit: 3 },
            }) satisfies ApiError,
        ),
      );
    await render();
    await choose(pdf(1024));
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'The file exceeds the 8 MB limit.',
    );
    await choose(pdf(1024));
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'You have reached the limit of 3 resumes. Delete one to upload another.',
    );
  });

  it('shows the invalid PDF message on 415 INVALID_PDF (CV-02)', async () => {
    api.upload.mockReturnValue(
      throwError(() => ({ code: 'INVALID_PDF', message: 'raw' }) satisfies ApiError),
    );
    await render();
    await choose(pdf(1024));
    expect(root.querySelector('[role="alert"]')?.textContent?.trim()).toBe(
      'Please upload a valid PDF file.',
    );
  });

  it('rejects an empty file before uploading (CV-90)', async () => {
    await render();
    await choose(pdf(0));
    expect(api.upload).not.toHaveBeenCalled();
    expect(root.querySelector('[role="alert"]')?.textContent?.trim()).toBe(
      'Please upload a valid PDF file.',
    );
  });

  it('accepts a file of exactly the limit and blocks one byte above before uploading (CV-91)', async () => {
    api.upload.mockReturnValue(of(makeResume({ status: 'received' })));
    await render();

    await choose(pdf(5 * 1024 * 1024 + 1));
    expect(api.upload).not.toHaveBeenCalled();
    expect(root.querySelector('[role="alert"]')?.textContent?.trim()).toBe(
      'The file exceeds the 5 MB limit.',
    );

    await choose(pdf(5 * 1024 * 1024));
    expect(api.upload).toHaveBeenCalledTimes(1);
  });

  it('marks the upload as busy while it is in flight', async () => {
    const pending = new Subject<ResumeSummary>();
    api.upload.mockReturnValue(pending.asObservable());
    await render();
    await choose(pdf(1024));

    const input = root.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input?.disabled).toBe(true);
    expect(text()).toContain('Uploading');

    pending.next(makeResume({ status: 'received' }));
    pending.complete();
    fixture.detectChanges();
    expect(input?.disabled).toBe(false);
  });

  it('opens the confirmation dialog with the section 9 text and deletes only after confirming (DATA-03)', async () => {
    api.list.mockReturnValue(of([makeResume({ id: 'r-1', filename: 'cv.pdf' })]));
    await render();

    buttonByText('Delete', rows()[0]).click();
    fixture.detectChanges();

    const dialog = root.querySelector('dialog');
    expect(dialog).not.toBeNull();
    expect(dialog?.textContent).toContain(DELETE_CONFIRMATION);
    expect(api.delete).not.toHaveBeenCalled();

    buttonByText('Delete', dialog as HTMLElement).click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(api.delete).toHaveBeenCalledWith('r-1');
    expect(root.querySelector('dialog')).toBeNull();
    expect(rows()).toHaveLength(0);
  });

  it('does not delete when the dialog is cancelled', async () => {
    api.list.mockReturnValue(of([makeResume()]));
    await render();

    buttonByText('Delete', rows()[0]).click();
    fixture.detectChanges();
    buttonByText('Cancel', root.querySelector('dialog') as HTMLElement).click();
    fixture.detectChanges();

    expect(api.delete).not.toHaveBeenCalled();
    expect(root.querySelector('dialog')).toBeNull();
    expect(rows()).toHaveLength(1);
  });

  it('shows an error and keeps the item when deletion fails', async () => {
    api.list.mockReturnValue(of([makeResume()]));
    api.delete.mockReturnValue(
      throwError(() => ({ code: 'NETWORK_ERROR', message: 'raw' }) satisfies ApiError),
    );
    await render();

    buttonByText('Delete', rows()[0]).click();
    fixture.detectChanges();
    buttonByText('Delete', root.querySelector('dialog') as HTMLElement).click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(rows()).toHaveLength(1);
    expect(root.querySelector('[role="alert"]')?.textContent).toContain(
      'Unable to reach the server.',
    );
  });

  it('renders file names as text, never as HTML', async () => {
    api.list.mockReturnValue(of([makeResume({ filename: '<img src=x onerror=alert(1)>.pdf' })]));
    await render();
    expect(rows()[0].querySelector('img')).toBeNull();
    expect(rows()[0].textContent).toContain('<img src=x onerror=alert(1)>.pdf');
  });
});
