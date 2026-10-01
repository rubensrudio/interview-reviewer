import { ChangeDetectionStrategy, Component } from '@angular/core';
import { RouterLink } from '@angular/router';

/** Terms version in force; must match the backend `terms_version` setting. */
export const TERMS_VERSION = 'terms-2026-09';

/**
 * Public Terms of Use. Reachable with or without a session: no guard, no API call.
 * Static English text only, so nothing external is rendered.
 */
@Component({
  selector: 'app-terms-page',
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <article aria-labelledby="terms-page-title">
      <h1 id="terms-page-title">Terms of Use</h1>
      <p class="version">Version {{ version }}</p>

      <section aria-labelledby="terms-service">
        <h2 id="terms-service">The service</h2>
        <p>
          Interview Reviewer helps you practise technical interviews. You upload your resume, enter
          the requirements of a job and answer one question per required skill. At the end you get
          a score for each answer, a technical match percentage and, for weaker answers, an
          explanation of the gap and a reference answer.
        </p>
      </section>

      <section aria-labelledby="terms-results">
        <h2 id="terms-results">What the results mean</h2>
        <p>
          The match percentage measures your performance in that session only. It is not a
          prediction of hiring and not a certification of competence.
        </p>
      </section>

      <section aria-labelledby="terms-account">
        <h2 id="terms-account">Your account</h2>
        <p>
          Keep your credentials private and only upload content you are allowed to share. You can
          delete your resumes, sessions or your whole account at any time.
        </p>
      </section>

      <section aria-labelledby="terms-privacy">
        <h2 id="terms-privacy">Your data</h2>
        <p>
          How we process, keep and delete your data is described in the
          <a routerLink="/privacy">Privacy Policy</a>.
        </p>
      </section>
    </article>
  `,
  styles: `
    :host {
      display: block;
      max-width: 42rem;
      margin: 0 auto;
      padding: 2rem 1rem;
      color: #1a1a1a;
      line-height: 1.5;
    }
    h1 {
      margin: 0 0 0.5rem;
      font-size: 1.75rem;
    }
    h2 {
      margin: 0 0 0.5rem;
      font-size: 1.25rem;
    }
    article {
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    section {
      display: flex;
      flex-direction: column;
      gap: 0.5rem;
    }
    p {
      margin: 0;
    }
    .version {
      color: #4b5563;
    }
    a {
      color: #1d4ed8;
    }
    a:focus-visible {
      outline: 3px solid #1d4ed8;
      outline-offset: 2px;
    }
  `,
})
export class TermsPage {
  protected readonly version = TERMS_VERSION;
}
