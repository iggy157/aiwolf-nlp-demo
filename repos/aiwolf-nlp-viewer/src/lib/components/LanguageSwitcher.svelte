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

<select
  class="select select-bordered select-sm"
  aria-label="Language"
  value={selectedLanguage}
  onchange={handleLanguageChange}
>
  {#each LANGUAGES as { code, label }}
    <option value={code}>{label}</option>
  {/each}
</select>
