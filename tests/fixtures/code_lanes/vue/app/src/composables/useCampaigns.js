import { ref } from 'vue'

export function useCampaigns() {
    const campaigns = ref([])
    const loading = ref(false)
    const load = async (params) => {
        loading.value = true
        campaigns.value = await fetchCampaigns(params)
        loading.value = false
    }
    return { campaigns, loading, load }
}

async function fetchCampaigns(params) {
    const res = await fetch('/api/campaigns?' + new URLSearchParams(params))
    return res.json()
}
