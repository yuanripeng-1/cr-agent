type SkillReviewFixtureProps = {
  html: string;
  onDelete: () => void;
};

// E2E run marker: 2-valid (code-reviewer Skill only, cywx/gpt-5.6-sol)
export function SkillReviewFixture({ html, onDelete }: SkillReviewFixtureProps) {
  return (
    <section>
      <article dangerouslySetInnerHTML={{ __html: html }} />
      <div onClick={onDelete}>Delete</div>
    </section>
  );
}
