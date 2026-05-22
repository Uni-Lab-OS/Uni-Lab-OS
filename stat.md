cd /Users/dp/software/GitHub/LeapLab &&
    source /Users/dp/miniforge3/etc/profile.d/conda.sh &&
    conda activate unilab &&
unilab --graph unilabos/test/experiments/fault_injection.json --config unilabos/test/experiments/fault_injection_config.py --ak a3d111bb-571a-4548-aa5d-c58ccca64466 --sk c2450c73-e84c-4319-b25f-b5cc4d575e7e --upload_registry --addr http://127.0.0.1:48197/api/v1
