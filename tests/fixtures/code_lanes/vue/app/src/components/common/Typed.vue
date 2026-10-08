<template>
    <span class="typed">{{ fmt(props.n) }}</span>
</template>

<script setup lang="ts">
interface Props {
    n: number
    unit?: string
}

const props = defineProps<Props>()

function fmt(n: number): string {
    return `${n}${props.unit ?? ''}`
}
</script>
