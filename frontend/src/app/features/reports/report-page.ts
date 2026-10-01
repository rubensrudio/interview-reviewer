import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';

import { ApiError, NETWORK_ERROR } from '../../core/http/api-error';
import { ReportApi, ReportContent, ReportItem } from './report-api';

const GENERIC_ERROR = 'Something went wrong. Please try again.';

/** Catalog 8.3 texts for the errors of the report route, keyed by `code`. */
const ERRORS: Readonly<Record<string, string>> = {
  REPORT_NOT_AVAILABLE: 'The report is not available for this interview.',
  RESOURCE_NOT_FOUND: 'Not found.',
  [NETWORK_ERROR]: 'Unable to reach the server.',
};

/** Section 9 labels shown on the report. */
export const NO_VERIFIED_SOURCE_LABEL = 'No verified source';
export const HYPOTHETICAL_EXAMPLE_LABEL = 'Hypothetical example';
export const NOT_EVALUATED_LABEL = 'Not evaluated in this session';
export const NO_UNSATISFACTORY_MESSAGE = 'No unsatisfactory items.';

function errorText(err: ApiError): string {
  return ERRORS[err.code] ?? GENERIC_ERROR;
}

/** True only for absolute http(s) URLs; anything else is shown as plain text, never linked. */
function isWebUrl(url: string): boolean {
  try {
    const protocol = new URL(url).protocol;
    return protocol === 'http:' || protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Report page of a completed session (EVAL-06..08, EVAL-15, EVAL-90, EVAL-91, DATA-02, EXPT-01).
 *
 * Renders the frozen `ReportContent` (CT-64) exactly as received: the percentage and averages are
 * decimal strings shown untouched, and which items are satisfactory comes from the backend.
 * External text is rendered by interpolation only.
 */
@Component({
  selector: 'app-report-page',
  imports: [DatePipe, NgTemplateOutlet],
  templateUrl: './report-page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
      max-width: 48rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
    }
    h1 {
      margin: 0 0 0.5rem;
      font-size: 1.75rem;
    }
    h2 {
      margin: 0 0 0.75rem;
      font-size: 1.25rem;
    }
    h3 {
      margin: 0 0 0.5rem;
      font-size: 1.05rem;
    }
    p {
      margin: 0;
    }
    a {
      color: #1d4ed8;
    }
    section {
      margin-bottom: 1.5rem;
    }
    .headline {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 0.75rem 1.5rem;
      margin-bottom: 1rem;
    }
    .percentage {
      font-size: 2rem;
      font-weight: 700;
    }
    .notice {
      padding: 1rem;
      border: 1px solid #c4c4c4;
      border-radius: 0.5rem;
      background: #f9fafb;
    }
    .disclaimer {
      margin-bottom: 1.5rem;
      color: #374151;
    }
    .error {
      padding: 0.75rem 1rem;
      border: 1px solid #b91c1c;
      border-radius: 0.375rem;
      color: #7f1d1d;
      background: #fef2f2;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      margin-top: 1rem;
    }
    ul.plain,
    ol.plain {
      margin: 0;
      padding: 0;
      list-style: none;
    }
    ol.plain > li,
    ul.plain > li.card {
      margin-bottom: 0.75rem;
      padding: 0.75rem 1rem;
      border: 1px solid #e5e7eb;
      border-radius: 0.5rem;
    }
    .text {
      margin-top: 0.25rem;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    dl {
      margin: 0.5rem 0 0;
    }
    dt {
      margin-top: 0.5rem;
      font-weight: 600;
    }
    dd {
      margin: 0.25rem 0 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .label {
      display: inline-block;
      margin: 0.25rem 0;
      padding: 0.125rem 0.5rem;
      border: 1px solid #92400e;
      border-radius: 999px;
      color: #78350f;
      background: #fffbeb;
      font-size: 0.875rem;
      font-weight: 600;
    }
    .source {
      margin-top: 0.5rem;
      overflow-wrap: anywhere;
    }
    .source .meta {
      color: #4b5563;
      font-size: 0.875rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th,
    td {
      padding: 0.5rem;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
    }
    .button-link,
    button {
      display: inline-flex;
      align-items: center;
      min-height: 2.75rem;
      padding: 0.5rem 1rem;
      border-radius: 0.375rem;
      font: inherit;
      cursor: pointer;
    }
    .button-link {
      border: 1px solid #1d4ed8;
      color: #ffffff;
      background: #1d4ed8;
      text-decoration: none;
    }
    .secondary {
      border: 1px solid #4b5563;
      color: #1a1a1a;
      background: #ffffff;
    }
    button:focus-visible,
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
    @media (max-width: 40rem) {
      .actions .button-link,
      .actions button {
        width: 100%;
        justify-content: center;
      }
    }
  `,
})
export class ReportPage implements OnInit {
  private readonly api = inject(ReportApi);
  private readonly destroyRef = inject(DestroyRef);
  private readonly route = inject(ActivatedRoute);
  private sessionId = '';
  private loadSub: Subscription | null = null;

  protected readonly report = signal<ReportContent | null>(null);
  protected readonly loading = signal(true);
  protected readonly loadError = signal<string | null>(null);
  protected readonly pdfUrl = signal('');

  protected readonly noVerifiedSourceLabel = NO_VERIFIED_SOURCE_LABEL;
  protected readonly hypotheticalExampleLabel = HYPOTHETICAL_EXAMPLE_LABEL;
  protected readonly notEvaluatedLabel = NOT_EVALUATED_LABEL;
  protected readonly noUnsatisfactoryMessage = NO_UNSATISFACTORY_MESSAGE;
  protected readonly isWebUrl = isWebUrl;

  protected readonly satisfactoryItems = computed(() =>
    (this.report()?.items ?? []).filter((item) => item.satisfactory),
  );
  /** Items listed by the backend in `unsatisfactory_items`, in that order. */
  protected readonly unsatisfactoryItems = computed(() => {
    const report = this.report();
    if (!report) {
      return [];
    }
    const byPosition = new Map(report.items.map((item) => [item.position, item]));
    return report.unsatisfactory_items
      .map((position) => byPosition.get(position))
      .filter((item): item is ReportItem => item !== undefined);
  });
  protected readonly hasNonEvaluated = computed(() => {
    const nonEvaluated = this.report()?.non_evaluated;
    return (
      !!nonEvaluated &&
      (nonEvaluated.nice_to_have.length > 0 || nonEvaluated.non_technical.length > 0)
    );
  });

  ngOnInit(): void {
    this.destroyRef.onDestroy(() => this.loadSub?.unsubscribe());
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      const id = params.get('id') ?? '';
      if (id !== this.sessionId) {
        this.sessionId = id;
        this.pdfUrl.set(this.api.pdfUrl(id));
        this.report.set(null);
        this.load();
      }
    });
  }

  protected reload(): void {
    this.load();
  }

  private load(): void {
    this.loadSub?.unsubscribe();
    this.loading.set(true);
    this.loadError.set(null);
    this.loadSub = this.api.get(this.sessionId).subscribe({
      next: (report) => {
        this.loading.set(false);
        this.report.set(report);
      },
      error: (err: ApiError) => {
        this.loading.set(false);
        this.report.set(null);
        this.loadError.set(errorText(err));
      },
    });
  }
}
