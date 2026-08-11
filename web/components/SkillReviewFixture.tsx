type SkillReviewFixtureProps = {
  html: string;
  onDelete: () => void;
};

// E2E run marker: 1 (dimensions only / code-reviewer matrix row)
export function SkillReviewFixture({ html, onDelete }: SkillReviewFixtureProps) {
  return (
    <section>
      <article dangerouslySetInnerHTML={{ __html: html }} />
      <div onClick={onDelete}>Delete</div>
    </section>
  );
}
