type SkillReviewFixtureProps = {
  html: string;
  onDelete: () => void;
};

// E2E run marker: 2-valid-retry (code-reviewer Skill only, worker c2f23ca)
export function SkillReviewFixture({ html, onDelete }: SkillReviewFixtureProps) {
  return (
    <section>
      <article dangerouslySetInnerHTML={{ __html: html }} />
      <div onClick={onDelete}>Delete</div>
    </section>
  );
}
