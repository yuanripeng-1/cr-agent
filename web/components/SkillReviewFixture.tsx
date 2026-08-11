type SkillReviewFixtureProps = {
  html: string;
  onDelete: () => void;
};

// E2E run marker: 4-valid (security dimension only; frontend matrix)
export function SkillReviewFixture({ html, onDelete }: SkillReviewFixtureProps) {
  return (
    <section>
      <article dangerouslySetInnerHTML={{ __html: html }} />
      <div onClick={onDelete}>Delete</div>
    </section>
  );
}
