# frozen_string_literal: true

require_relative "memory_step"

execute do
  ruby(:memory) do
    RoastMemory.run(ENV.fetch("ROAST_MEMORY_ACTION", "recall"))
  end

  ruby(:report) do
    receipt = ruby!(:memory).value
    puts JSON.generate(receipt)
    raise RoastMemory::StepFailed, "Memory step failed" if receipt["status"] == "error"

    receipt
  end
end
