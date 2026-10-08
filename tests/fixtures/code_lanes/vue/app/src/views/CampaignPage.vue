<template>
    <div class="page">
        <DataView :loading="loading">
            <template #header>
                <Button label="Refresh" @click="reload" />
                <Button label="Clear" @click="clearFilters()" />
            </template>
            <ul>
                <li v-for="row in campaigns" :key="row.id">{{ formatRow(row) }}</li>
            </ul>
        </DataView>
        <data-view v-if="empty" />
        <router-link :to="{ name: 'campaigns.new' }">New</router-link>
    </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { Button } from 'primevue'

import DataView from '@/components/common/DataView.vue'
import { useCampaigns } from '@/composables/useCampaigns'

const { campaigns, loading, load } = useCampaigns()

const empty = computed(() => campaigns.value.length === 0)

const reload = async () => {
    await load({ limit: 50 })
}

const clearFilters = () => {
    reload()
}

const formatRow = (row) => `${row.name} (${row.status})`

onMounted(reload)
</script>
