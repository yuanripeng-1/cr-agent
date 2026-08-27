type SkillReviewFixtureProps = {
  html: string;
  onDelete: () => void;
};

// E2E run marker: skills-only previous-issue re-review, round 2
export function SkillReviewFixture({ html, onDelete }: SkillReviewFixtureProps) {
  return (
    <section>
      <article dangerouslySetInnerHTML={{ __html: html }} />
      <button type="button" onClick={onDelete}>Delete</button>
    </section>
  );
}
