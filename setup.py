from setuptools import find_packages, setup

package_name = 'unilabos'

setup(
    name=package_name,
    version='0.11.3',
    packages=find_packages(),
    include_package_data=True,
    install_requires=[
        'setuptools',
        'jsonschema>=4.18',
        'rfc8785>=0.1.4,<0.2',
        'msgcenterpy>=0.1.8,<0.2',
        'pylabrobot==0.2.1',
    ],
    extras_require={
        'mcp': ['mcp>=1.10,<2'],
    },
    zip_safe=True,
    author="The unilabos developers",
    maintainer='Junhan Chang, Xuwznln',
    maintainer_email='Junhan Chang <changjh@pku.edu.cn>, Xuwznln <18435084+Xuwznln@users.noreply.github.com>',
    description='',
    license='GPL v3',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            "unilab = unilabos.app.main:main",
            "unilab-supervisor = unilabos.managed_runtime.supervisor:main",
            "unilab-mcp = unilabos.agent_tools.workflow:main",
        ],
    },
)
