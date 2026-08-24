#!/usr/bin/env bash

# compare this two big files 9mb
# https://cdnjs.cloudflare.com/ajax/libs/typescript/5.4.5/typescript.js
# https://cdnjs.cloudflare.com/ajax/libs/typescript/5.9.2/typescript.js
#
# https://github.com/microsoft/TypeScript/releases/download/v5.7.2/typescript-5.7.2.tgz
# https://github.com/microsoft/TypeScript/releases/download/v6.0.3/typescript-6.0.3.tgz
# extract and compare lib/typescript.js

# compare this two big minified files 3mb - big line
# https://cdnjs.cloudflare.com/ajax/libs/typescript/5.4.5/typescript.min.js
# https://cdnjs.cloudflare.com/ajax/libs/typescript/5.9.2/typescript.min.js

declare -A FILES=(
    ["autogen_test_5a_jquery-3.6.0.js"]="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.6.0/jquery.js"
    ["autogen_test_5b_jquery-4.0.0.js"]="https://cdnjs.cloudflare.com/ajax/libs/jquery/4.0.0/jquery.js"
    ["autogen_test_4a_jquery-3.6.0.min.js"]="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.6.0/jquery.min.js"
    ["autogen_test_4b_jquery-4.0.0.min.js"]="https://cdnjs.cloudflare.com/ajax/libs/jquery/4.0.0/jquery.min.js"
    ["autogen_test_3a_typescript.min5.4.5.js"]="https://cdnjs.cloudflare.com/ajax/libs/typescript/5.4.5/typescript.min.js"
    ["autogen_test_3b_typescript.min5.9.2.js"]="https://cdnjs.cloudflare.com/ajax/libs/typescript/5.9.2/typescript.min.js"
    ["autogen_test_2a_typescript5.4.5.js"]="https://unpkg.com/typescript@5.4.5/lib/typescript.js"
    ["autogen_test_2b_typescript5.9.2.js"]="https://unpkg.com/typescript@5.9.2/lib/typescript.js"
    ["autogen_test_1a_typescript_5.7.2.js"]="https://unpkg.com/typescript@5.7.2/lib/typescript.js"
    ["autogen_test_1b_typescript_6.0.3.js"]="https://unpkg.com/typescript@6.0.3/lib/typescript.js"
)

echo "_________Starting download of assets..."

for filename in "${!FILES[@]}"; do
    url="${FILES[$filename]}"    
    echo "Downloading: $filename"
    [[ -f $filename ]] || curl -sL "$url" -o "$filename"
done

echo "All downloads completed successfully!"



echo "_________create random big files..."
python ./_annex/create_big_random_file.py

echo
read -p "endddd"
echo
