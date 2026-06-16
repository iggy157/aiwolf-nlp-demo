<script lang="ts">
  import { LANGUAGES, language, type Language } from "$lib/stores/language";
  import { onDestroy } from "svelte";

  let selectedLanguage = $state<Language>("ja");

  const unsubscribe = language.subscribe((lang) => {
    selectedLanguage = lang;
  });

  onDestroy(() => {
    unsubscribe();
  });

  function handleLanguageChange(event: Event): void {
    const target = event.target as HTMLSelectElement;
    language.set(target.value as Language);
  }
</script>

<!-- 翻訳アイコン(文A)を添えて「言語切替」と一目で分かるようにする（言語非依存の目印）。 -->
<label class="flex items-center gap-1" title="Language" aria-label="Language">
  <iconify-icon icon="mdi:translate" class="text-base opacity-70"></iconify-icon>
  <select
    class="select select-bordered select-sm"
    value={selectedLanguage}
    onchange={handleLanguageChange}
  >
    {#each LANGUAGES as { code, label }}
      <option value={code}>{label}</option>
    {/each}
  </select>
</label>
