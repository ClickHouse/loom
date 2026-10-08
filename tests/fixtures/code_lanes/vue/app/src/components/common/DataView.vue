<template>
    <!-- A bordered data panel with a collapsible body. -->
    <div class="data-view" :class="{ collapsed }">
        <header @click="toggle">
            <slot name="header" />
        </header>
        <section v-if="!collapsed">
            <slot />
        </section>
    </div>
</template>

<script setup>
import { ref } from 'vue'

const props = defineProps({
    loading: { type: Boolean, default: false },
    collapsible: { type: Boolean, default: true },
})

const collapsed = ref(false)

const toggle = () => {
    if (props.collapsible) collapsed.value = !collapsed.value
}
</script>

<style scoped>
.data-view { border: 1px solid var(--border); }
</style>
